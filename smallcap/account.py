"""唯一股数现金账本：事件投影、精确份额化、委托成交入账、费用、税批次、公司行动（D07.1、D11、D12、H04、H07）。

`Ledger.apply(event)` 是唯一允许改变账户的入口（D15.1 apply_confirmed_event）。同 event_id 同内容幂等，
同 event_id 异内容为硬冲突并停止增风险。所有硬比例使用 Fraction；金额为有限十进制。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction

from .contracts import (DD_LIMIT, DD_RETIRE, DD_WARNING, DETAIL_VERSION, SH_TZ, WINDOW_END, WINDOW_START,
                        ContractError, cents, dec_str, money, parse_date, parse_ts, ratio, shares, unwire_ratio,
                        wire_ratio)
from .fees import FeeSchedule, allocate_cents, allocate_int
from .orders import RECONCILE_REQUIRED, Order

MARK_OK = ("NORMAL", "PROVISIONAL_SUSPENDED", "PROVISIONAL_REFERENCE")


def canon(obj) -> str:
    """事件内容指纹（仅用于同ID同内容的幂等判断，不是源码/数据全量哈希体系）。"""
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


@dataclass
class Position:
    security_id: str
    market: str
    board: str
    industry: str | None = None
    quantity: int = 0
    pending: list = field(default_factory=list)          # [[sellable_from(YYYY-MM-DD), qty]]
    broker_can_sell: int | None = None                   # 券商返回净可卖（前向模式）
    mark: Decimal | None = None
    mark_date: str | None = None
    mark_status: str = "UNRELIABLE"
    mark_bounds: list | None = None                      # 暂估上下界 [lo, hi]（可选）
    lifecycle: str = "NORMAL"                            # NORMAL / RESIDUAL
    lots: list = field(default_factory=list)
    share_receivable: int = 0
    share_receivable_unknown: bool = False
    owner: str = "STOCK"
    nonorder_frozen: int = 0                             # 非订单原因的股份冻结（本地镜像）
    nonorder_frozen_reflected: bool = True               # 是否已体现在 broker_can_sell 中

    def mature_quantity(self, asof: str) -> int:
        """已满足交收/上市条件的总可转让股份（尚未扣订单预约）。"""
        return self.quantity - sum(q for d, q in self.pending if d > asof)

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["mark"] = dec_str(self.mark) if self.mark is not None else None
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Position":
        d = dict(d)
        d["mark"] = money(d["mark"]) if d["mark"] is not None else None
        return cls(**d)


class Ledger:
    """单账户（本股票策略视图）的唯一投影。"""

    def __init__(self, account_id: str, fee_schedule: FeeSchedule | None = None, etf_ids=(),
                 strategy_id: str = "SMALLCAP"):
        self.account_id = account_id
        self.strategy_id = strategy_id
        self.fees = fee_schedule
        self.etf_ids = frozenset(etf_ids)
        self.cash = Decimal(0)
        self.restrictions: dict = {}         # id -> {amount, until, reason}（非订单限制）
        self.receivables: dict = {}          # key -> Decimal（分红应收、退款等）
        self.fee_payables: dict = {}         # order_id/key -> Decimal
        self.tax_payables: dict = {}         # slice_id -> Decimal（负数为应退）
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self.entitlements: dict = {}         # action_id -> 权益记录
        self.slices: dict = {}               # slice_id -> 税关系
        self.units = Fraction(0)
        self.hwm = Fraction(0)
        self.last_nav = Fraction(0)
        self.capital_started = False
        self.capital_terminated = False
        self.capital_revision = 0
        self.capital_flows: list = []
        self.capacity_used: dict = {}        # "sid|date" -> Decimal
        self.ledger_points: list = []
        self.run_state = "WAITING_START"
        self.retirement_triggered = False
        self.retirement_trigger_at: str | None = None
        self.retirement_manually_cleared: list = []
        self.episodes: dict = {}
        self.stress_blocks: list = []
        self.counters: dict = {}
        self.counter_decisions: list = []
        self.plans: dict = {}
        self.current_plan_id: str | None = None
        self.week_stats: list = []
        self.cursor: dict | None = None
        self.limitations: dict = {}          # code -> detail（账户级粘滞限制）
        self.seq = 0
        self.revision = 0
        self.projection_revision = "P0"
        self.seen: dict = {}
        self.trade_log: list = []            # 成交摘要（换手/费用统计）
        self.decision_log: list = []
        self.lot_seq = 0
        self.binding: dict | None = None     # 运行身份（StudyDefinition/单元/输入内容），恢复时必须一致
        self._open_ids: set = set()          # 未结订单索引（派生，可由 orders 重建）
        self._touched: set = set()

    # ================================================================ 查询
    def pos(self, sid: str) -> Position | None:
        return self.positions.get(sid)

    def equity(self, prices: dict | None = None) -> tuple[Fraction, str]:
        """E = C_book + Σ(q+股份应收)×m + 应收 − 应付；返回 (精确值, 估值状态)。
        prices 为同一一致估值时点的可信原始价覆盖（执行前复核用），未覆盖者用账本估值。"""
        status = "OK"
        e = Fraction(self.cash)
        for sid, p in self.positions.items():
            q = p.quantity + p.share_receivable
            if q == 0:
                continue
            if prices is not None and sid in prices and not p.share_receivable_unknown:
                e += q * Fraction(prices[sid])
                continue
            if p.mark is None or p.mark_status not in MARK_OK or p.share_receivable_unknown:
                status = "UNKNOWN"
                continue
            if p.mark_status != "NORMAL" and status == "OK":
                status = "PROVISIONAL"
            e += q * Fraction(p.mark)
        e += sum((Fraction(v) for v in self.receivables.values()), Fraction(0))
        e -= sum((Fraction(v) for v in self.fee_payables.values()), Fraction(0))
        e -= sum((Fraction(v) for v in self.tax_payables.values()), Fraction(0))
        return e, status

    def position_value(self, sid: str, price: Decimal | None = None) -> Fraction:
        p = self.positions[sid]
        m = p.mark if price is None else price
        if m is None:
            raise ContractError("VALUATION_UNKNOWN", sid)
        return (p.quantity + p.share_receivable) * Fraction(m)

    def stock_value(self) -> Fraction:
        return sum((self.position_value(s) for s, p in self.positions.items()
                    if p.quantity + p.share_receivable > 0), Fraction(0))

    def industry_values(self) -> dict:
        out: dict = {}
        for s, p in self.positions.items():
            if p.quantity + p.share_receivable > 0:
                out[p.industry] = out.get(p.industry, Fraction(0)) + self.position_value(s)
        return out

    def nav(self) -> Fraction | None:
        if self.units == 0:
            return self.last_nav if self.capital_started else None
        e, st = self.equity()
        if st == "UNKNOWN":
            return None
        return e / self.units

    def open_orders(self, sid: str | None = None, side: str | None = None) -> list[Order]:
        out = []
        for coid in sorted(self._open_ids):
            o = self.orders[coid]
            if (sid is None or o.security_id == sid) and (side is None or o.side == side):
                out.append(o)
        return out

    def _sync_open(self) -> None:
        self._open_ids = {k for k in self._open_ids | self._touched if k in self.orders and self.orders[k].open}
        self._touched = set()

    def reserved_sell(self, sid: str, only_unsubmitted: bool = False) -> int:
        return sum(o.remaining_possible for o in self.open_orders(sid, "SELL")
                   if not only_unsubmitted or not o.submitted)

    def free_sell(self, sid: str, asof: str) -> int:
        """free_sell = min(mature−全部本地未结卖量预约, broker_can_sell−已批准未反映券商冻结的卖量)，均不为负。"""
        p = self.positions.get(sid)
        if p is None or p.quantity == 0:
            return 0
        a = p.mature_quantity(asof) - self.reserved_sell(sid) - p.nonorder_frozen
        if p.broker_can_sell is None:
            return max(a, 0)
        b = p.broker_can_sell - self.reserved_sell(sid, only_unsubmitted=True) - (
            0 if p.nonorder_frozen_reflected else p.nonorder_frozen)
        return max(min(a, b), 0)

    def buy_cash_reserved(self, order: Order) -> Decimal:
        rem = order.remaining_possible
        if order.side != "BUY" or rem == 0:
            return Decimal(0)
        possible = rem * order.limit
        extra = Decimal(0)
        if self.fees is not None:
            fills = [(money(f["amount"]), f["qty"]) for f in order.fills.values()]
            accrued = sum(order.fees_accrued.values(), Decimal(0))
            extra = self.fees.extra_fee_reserve(order.market, "BUY", parse_date(order.trade_date),
                                                order.filled_value, order.filled_qty, possible, rem, accrued, fills)
        return possible + extra

    def cash_commitments(self) -> Decimal:
        return sum((self.buy_cash_reserved(o) for o in self.open_orders()), Decimal(0))

    def restricted_cash(self, asof: str) -> Decimal:
        return sum((money(r["amount"]) for r in self.restrictions.values() if r["until"] is None or r["until"] > asof),
                   Decimal(0))

    def due_cash(self) -> Decimal:
        """已知待付款（应付费用与税负，正值部分）。"""
        return (sum((v for v in self.fee_payables.values() if v > 0), Decimal(0))
                + sum((v for v in self.tax_payables.values() if v > 0), Decimal(0)))

    def own_free_cash(self, asof: str, contingent_reserve: Decimal | None = None) -> Decimal:
        """策略可授权额（D14.2）：own_cash_book − 非订单限制 − 未结承诺 − 待付 − 或有税预留。"""
        cr = self.contingent_tax_reserve(asof) if contingent_reserve is None else contingent_reserve
        return self.cash - self.restricted_cash(asof) - self.cash_commitments() - self.due_cash() - cr

    def actual_slots(self) -> set:
        """真实证券槽 = 非零正常仓 ∪ 未结束新买证券。"""
        held = {s for s, p in self.positions.items() if p.quantity + p.share_receivable > 0 and p.lifecycle == "NORMAL"}
        buys = {o.security_id for o in self.open_orders(side="BUY") if o.remaining_possible > 0}
        return held | buys

    def capacity_pending(self, sid: str, d: str) -> Decimal:
        tot = Decimal(0)
        for o in self.open_orders(sid):
            if o.trade_date != d:
                continue
            rem = o.remaining_possible
            if o.side == "BUY":
                tot += rem * o.limit
            else:
                if o.upper_price_bound is None:
                    raise ContractError("CAPACITY_PRICE_BOUND_UNKNOWN", o.client_order_id)
                tot += rem * money(o.upper_price_bound)
        return tot

    def capacity_used_on(self, sid: str, d: str) -> Decimal:
        return self.capacity_used.get(f"{sid}|{d}", Decimal(0))

    # ---------------------------------------------------------------- 税
    def contingent_tax_reserve(self, asof: str) -> Decimal:
        """未卖分红批次若今日全部依法转让的补税（仅限制购买力，不进入E）。"""
        total = Decimal(0)
        if self.fees is None:
            return total
        d = parse_date(asof)
        for s in self.slices.values():
            if s["unresolved_qty"] <= 0 or s.get("tax_unknown") or s["selected_rule_id"] is None:
                continue
            lot = self._lot(s["security_id"], s["lot_id"])
            if lot is None or lot["acquisition_tax_date"] is None:
                continue
            rule = self.fees.tax.by_id(s["selected_rule_id"])
            rate = rule.final_rate(parse_date(lot["acquisition_tax_date"]), d)
            due = cents(s["unresolved_qty"] * money(s["tax_base_per_share"]) * rate) - money(s["withheld_unapplied"])
            total += max(due, Decimal(0))
        return total

    def preview_sale_tax(self, sid: str, qty: int, delivery: str) -> Decimal | None:
        """D07.4/D11.4：卖出 qty 股（FIFO）在 delivery 交割时新增的股息差别税（最终税−对应已预扣），
        与实际成交入账同一取整规则；在副本上计算，不改变账本。税制、税基或取得日未知返回 None（不得按0处理）。"""
        if qty <= 0:
            return Decimal(0)
        p = self.positions.get(sid)
        if p is None or self.fees is None:
            return None
        if sum(l["remaining_qty"] for l in p.lots) < qty:
            return None
        if not any(v["security_id"] == sid and v["unresolved_qty"] > 0 for v in self.slices.values()):
            return Decimal(0)                         # 无尚未了结的分红税关系
        fake = copy.copy(p)
        fake.lots = copy.deepcopy(p.lots)
        slices = {k: copy.deepcopy(v) for k, v in self.slices.items() if v["security_id"] == sid}
        payables, lim = {}, {}
        self._dispose_tax(fake, self._consume_lots(fake, qty), delivery, "PREVIEW", slices, payables, lim)
        if lim:
            return None
        return sum(payables.values(), Decimal(0))

    def _lot(self, sid: str, lot_id: str) -> dict | None:
        p = self.positions.get(sid)
        if p is None:
            return None
        for lot in p.lots:
            if lot["lot_id"] == lot_id:
                return lot
        return None

    # ================================================================ 事件入口
    def apply(self, ev: dict) -> bool:
        """应用一条已确认事件。返回 True 表示有新变化，False 表示完全重复。"""
        eid = ev["event_id"]
        content = canon({"kind": ev["kind"], "payload": ev["payload"]})
        if eid in self.seen:
            if self.seen[eid] == content:
                return False
            self.limitations["EVENT_CONFLICT"] = eid
            raise ContractError("EVENT_CONFLICT", eid)
        handler = getattr(self, "_on_" + ev["kind"].lower(), None)
        if handler is None:
            raise ContractError("UNKNOWN_EVENT_KIND", ev["kind"])
        pl = ev["payload"]
        coid = pl.get("client_order_id") or (pl.get("order") or {}).get("client_order_id")
        try:
            handler(pl, ev)
        finally:
            if coid:
                self._touched.add(coid)
            self._sync_open()
        self.seen[eid] = content
        self.seq += 1
        self.revision += 1
        return True

    # ---------------------------------------------------------------- 资本流
    def _on_capital_flow(self, p: dict, ev: dict) -> None:
        f = money(p["amount"])
        ff = Fraction(f)
        if not self.capital_started:
            if f <= 0:
                raise ContractError("CAPITAL_INVALID", "initial flow must be positive")
            self.cash += f
            self.units = ff
            self.hwm = Fraction(1)
            self.last_nav = Fraction(1)
            self.capital_started = True
        else:
            if self.capital_terminated:
                raise ContractError("CAPITAL_TERMINATED")
            e, st = self.equity()
            if st == "UNKNOWN":
                raise ContractError("VALUATION_UNKNOWN", "资本流需要可信权益")
            if e <= 0 or self.units <= 0:
                raise ContractError("CAPITAL_INVALID", "E<=0 或 U<=0")
            if e + ff < 0:
                raise ContractError("CAPITAL_INVALID", "E+F<0")
            if f < 0:
                # 转出只能动用已确认、无占用、覆盖待付需要的现金（D12.5、D14.4）
                asof = (ev.get("occurred_at") or "")[:10] or "9999-12-31"
                free = self.own_free_cash(asof)
                if -f > free:
                    if not p.get("confirmed_external"):
                        raise ContractError("CAPITAL_OUTFLOW_EXCEEDS_FREE_CASH", f"{-f}>{free}")
                    # 券商已确认的真实出金：照实入账，记录政策违规并停止新增
                    self.limitations[f"CAPITAL_FLOW_POLICY_BREACH:{p['flow_id']}"] = f"{-f}>{free}"
            v = e / self.units
            self.units = self.units * (e + ff) / e
            self.cash += f
            self.last_nav = v
            if self.units == 0:
                self.capital_terminated = True
            elif self.equity()[0] / self.units != v:
                raise ContractError("CAPITAL_UNIT_VALUE_CHANGED")
        self.capital_revision += 1
        self.capital_flows.append({"flow_id": p["flow_id"], "amount": dec_str(f), "at": ev.get("occurred_at"),
                                   "transfer_id": p.get("transfer_id")})

    # ---------------------------------------------------------------- 估值
    def _on_marks(self, p: dict, ev: dict) -> None:
        for sid, m in p["marks"].items():
            pos = self.positions.get(sid)
            if pos is None:
                continue
            pos.mark = money(m["price"]) if m.get("price") is not None else None
            pos.mark_date = m.get("mark_date")
            pos.mark_status = m["status"]
            pos.mark_bounds = m.get("bounds")
            if m.get("industry") is not None:
                pos.industry = m["industry"]

    def _on_eod(self, p: dict, ev: dict) -> None:
        d = p["date"]
        pending_ca = {e["security_id"] for e in self.entitlements.values()
                      if e["eligible_qty"] and not self._entitlement_done(e)}
        for sid in list(self.positions):
            pos = self.positions[sid]
            pos.pending = [x for x in pos.pending if x[0] > d]
            if pos.quantity == 0 and pos.share_receivable == 0 and not pos.lots and not self.open_orders(sid) \
                    and sid not in pending_ca:
                del self.positions[sid]
        self.capacity_used = {k: v for k, v in self.capacity_used.items() if k.split("|")[1] >= d}
        e, st = self.equity()
        point = {"date": d, "equity": e, "valuation_status": st, "units": self.units, "run_state": self.run_state,
                 "cash": self.cash, "stock_value": None, "receivables": sum(self.receivables.values(), Decimal(0)),
                 "liabilities": self.due_cash(), "insolvent": False}
        if p.get("frozen_exit_value") is not None:
            point["frozen_exit_value"] = unwire_ratio(p["frozen_exit_value"])
        if st != "UNKNOWN":
            point["stock_value"] = self.stock_value()
            normal, prov, prov_lo, prov_hi, names = Fraction(0), Fraction(0), Fraction(0), Fraction(0), 0
            hi_known = True
            for sid2, pos2 in self.positions.items():
                q2 = pos2.quantity + pos2.share_receivable
                if q2 == 0:
                    continue
                v2 = q2 * Fraction(pos2.mark)
                if pos2.lifecycle == "NORMAL":
                    normal += v2
                    names += 1
                if pos2.mark_status != "NORMAL":
                    prov += v2
                    b = pos2.mark_bounds
                    prov_lo += q2 * Fraction(money(b[0])) if b else Fraction(0)
                    if b and b[1] is not None:
                        prov_hi += q2 * Fraction(money(b[1]))
                    else:
                        hi_known = False
            point.update(normal_stock_value=normal, names=names, provisional_value=prov, provisional_lo=prov_lo,
                         provisional_hi=prov_hi if hi_known else None)
        if self.units > 0 and st != "UNKNOWN":
            nav = e / self.units
            self.last_nav = nav
            self.hwm = max(self.hwm, nav)
            point.update(nav=nav, hwm=self.hwm, dd=1 - nav / self.hwm, insolvent=e <= 0)
        elif self.units == 0 and self.capital_started:
            point.update(nav=self.last_nav, hwm=self.hwm, dd=1 - self.last_nav / self.hwm if self.hwm else Fraction(0))
        else:
            point.update(nav=None, hwm=self.hwm if self.hwm else None, dd=None)
        self.ledger_points.append(point)

    # ---------------------------------------------------------------- 委托
    def _order(self, coid: str) -> Order:
        if coid not in self.orders:
            raise ContractError("UNKNOWN_ORDER", coid)
        return self.orders[coid]

    def _on_order_create(self, p: dict, ev: dict) -> None:
        o = p["order"]
        if o["client_order_id"] in self.orders:
            raise ContractError("ORDER_ID_REUSED", o["client_order_id"])
        for x in self.open_orders(o["security_id"]):
            if x.side == o["side"]:
                raise ContractError("DUPLICATE_OPEN_ORDER", f"{o['security_id']}/{o['side']}")
            raise ContractError("CROSS_OPEN_ORDER", f"{o['security_id']} 同批买卖不交叉未结")
        order = Order(client_order_id=o["client_order_id"], batch_id=o["batch_id"], plan_id=o.get("plan_id", ""),
                      security_id=o["security_id"], market=o["market"], board=o["board"], side=o["side"],
                      qty=shares(o["qty"]), limit=money(o["limit"]), kind=o["kind"], trade_date=o["trade_date"],
                      capital_revision=o["capital_revision"], created_at=ev.get("occurred_at", ""),
                      window_end=o["window_end"], attempt=o.get("attempt", 1),
                      intent_reason=o.get("intent_reason", ""), upper_price_bound=o.get("upper_price_bound"))
        if order.qty <= 0:
            raise ContractError("ORDER_QTY_INVALID", order.client_order_id)
        if order.side == "SELL" and order.qty > self.free_sell(order.security_id, order.trade_date):
            raise ContractError("OVERSELL_PREVENTED", order.client_order_id)
        if order.side == "BUY" and order.security_id not in self.positions:
            self.positions[order.security_id] = Position(order.security_id, order.market, order.board,
                                                         o.get("industry"))
        order.reserve()
        self.orders[order.client_order_id] = order

    def _on_order_local_cancel(self, p: dict, ev: dict) -> None:
        self._order(p["client_order_id"]).local_cancel(p["reason"])

    def _on_order_submit(self, p: dict, ev: dict) -> None:
        self._order(p["client_order_id"]).submit(p["at"])

    def _on_broker_ack(self, p: dict, ev: dict) -> None:
        self._order(p["client_order_id"]).ack(p.get("broker_order_id"))

    def _on_cancel_request(self, p: dict, ev: dict) -> None:
        self._order(p["client_order_id"]).request_cancel(p["request_id"], p["reason"])

    def _on_cancel_failed(self, p: dict, ev: dict) -> None:
        o = self._order(p["client_order_id"])
        o.cancel_failed(p.get("original_still_open"), self.revision + 1)
        self._order_limitations(o)

    def _on_broker_terminal(self, p: dict, ev: dict) -> None:
        o = self._order(p["client_order_id"])
        o.terminal_evidence(p["status"], p["cum_qty"], money(p["cum_value"]), int(p["source_revision"]))
        self._order_limitations(o)

    def _on_order_resolve(self, p: dict, ev: dict) -> None:
        o = self._order(p["client_order_id"])
        o.resolve(p["evidence_ref"])
        self._order_limitations(o)

    def _order_limitations(self, o: Order) -> None:
        key = f"ORDER_RECONCILE:{o.client_order_id}"
        if o.status == RECONCILE_REQUIRED:
            self.limitations[key] = o.status
        else:
            self.limitations.pop(key, None)
        if o.breach:
            self.limitations[f"CONFIRMED_EXECUTION_BREACH:{o.client_order_id}"] = "breach"

    def _on_broker_fill(self, p: dict, ev: dict) -> None:
        o = self._order(p["client_order_id"])
        qty, price = shares(p["qty"]), money(p["price"])
        amount = qty * price if p.get("amount") is None else money(p["amount"])
        if o.side == "SELL":
            pos = self.positions.get(o.security_id)
            if pos is None or qty > pos.quantity:
                raise ContractError("OVERSELL_CONFIRMED", f"{o.security_id}: 成交卖量超过真实持股")
        result = o.add_fill(p["trade_key"], qty, price, p.get("amount"), p["occurred_at"], p["received_at"])
        if result != "NEW":
            self._order_limitations(o)
            return
        d = o.trade_date if p.get("trade_date") is None else p["trade_date"]
        # 费用：同一委托累计差额一次计提
        fee_delta = Decimal(0)
        if self.fees is not None:
            try:
                fills = [(money(f["amount"]), f["qty"]) for f in o.fills.values()]
                tot = self.fees.order_fees(o.market, o.side, parse_date(d), o.filled_value, o.filled_qty, fills)
                for k, v in tot.items():
                    delta = v - o.fees_accrued.get(k, Decimal(0))
                    o.fees_accrued[k] = v
                    fee_delta += delta
            except ContractError as e:
                o.fee_unknown = True
                self.limitations[f"FEE_UNKNOWN:{o.client_order_id}"] = e.code
        else:
            o.fee_unknown = True
            self.limitations[f"FEE_UNKNOWN:{o.client_order_id}"] = "FEE_PROFILE_MISSING"
        if p.get("fee_settlement", "WITH_TRADE") == "WITH_TRADE":
            self.cash -= fee_delta
        else:
            self.fee_payables[o.client_order_id] = self.fee_payables.get(o.client_order_id, Decimal(0)) + fee_delta
        pos = self.positions.setdefault(o.security_id, Position(o.security_id, o.market, o.board))
        key = f"{o.security_id}|{d}"
        self.capacity_used[key] = self.capacity_used.get(key, Decimal(0)) + amount
        if o.side == "BUY":
            pos.quantity += qty
            self.cash -= amount
            if pos.mark is None or pos.mark_status not in MARK_OK:
                pos.mark, pos.mark_date, pos.mark_status = price, d, "NORMAL"   # 真实成交价作为当时可信估值
            sell_from = p.get("sellable_from")
            if sell_from is None:
                raise ContractError("SETTLEMENT_UNKNOWN", "buy fill requires sellable_from")
            if sell_from > d:
                pos.pending.append([sell_from, qty])
            self.lot_seq += 1
            pos.lots.append({"lot_id": f"{o.security_id}/L{self.lot_seq}", "acquisition_trade_id": p["trade_key"],
                             "acquisition_tax_date": p.get("tax_acquisition_date", d), "seq": self.lot_seq,
                             "original_qty": qty, "remaining_qty": qty, "economic_cost": dec_str(amount),
                             "parent_lot_id": None})
        else:
            disposals = self._consume_lots(pos, qty)
            pos.quantity -= qty
            self.cash += amount
            if p.get("proceeds_tradeable_from") and p["proceeds_tradeable_from"] > d:
                self.restrictions[f"PROCEEDS:{p['trade_key']}"] = {"amount": dec_str(amount),
                                                                   "until": p["proceeds_tradeable_from"],
                                                                   "reason": "SELL_PROCEEDS_NOT_TRADEABLE"}
            self._dispose_tax(pos, disposals, p.get("tax_delivery_date", d), p["trade_key"])
        self.trade_log.append({"date": d, "sid": o.security_id, "side": o.side, "qty": qty, "amount": dec_str(amount),
                               "fee": dec_str(fee_delta), "order": o.client_order_id, "occurred_at": p["occurred_at"],
                               "received_at": p["received_at"]})
        lt = parse_ts(p["occurred_at"]).astimezone(SH_TZ).time()
        if not (WINDOW_START <= lt < WINDOW_END) and p.get("window_checked", True):
            # 真实成交发生在窗口外：照实记录为窗口偏离，不回填10:29
            o.observations.append({"kind": "WINDOW_DEVIATION", "occurred_at": p["occurred_at"],
                                   "trade_key": p["trade_key"]})
        self._order_limitations(o)

    def _on_broker_cumulative(self, p: dict, ev: dict) -> None:
        """只有累计查询结果时：已核验的累计差额形成一笔确定差额成交（不重复记整个累计）。"""
        o = self._order(p["client_order_id"])
        cq, cv = shares(p["cum_qty"]), money(p["cum_value"])
        if cq < o.filled_qty or (cq == o.filled_qty and cv != o.filled_value):
            o.sticky_conflicts.append(f"CUMULATIVE_CONTRADICTS:{cq}")
            o.derive()
            self._order_limitations(o)
            return
        if cq == o.filled_qty:
            return
        dq, dv = cq - o.filled_qty, cv - o.filled_value
        price = dv / dq
        fp = dict(p.get("fill_fields", {}))
        fp.update({"client_order_id": o.client_order_id, "trade_key": f"CUM:{o.client_order_id}:{cq}", "qty": dq,
                   "price": dec_str(price), "amount": dec_str(dv), "occurred_at": p["observed_at"],
                   "received_at": p["observed_at"], "trade_date": o.trade_date, "window_checked": False})
        self._on_broker_fill(fp, ev)

    def _on_fee_settlement(self, p: dict, ev: dict) -> None:
        """assessed−already_accrued 改变净值；现金按真实结算变化；应付=已评未付。"""
        key = p["client_order_id"]
        o = self._order(key)
        accrued = sum(o.fees_accrued.values(), Decimal(0))
        if money(p["already_accrued"]) != accrued:
            raise ContractError("FEE_ACCRUAL_MISMATCH", key)
        assessed, cash_change = money(p["assessed_fee"]), money(p["settlement_cash_change"])
        paid_before = accrued - self.fee_payables.get(key, Decimal(0))
        if money(p["already_paid"]) != paid_before:
            raise ContractError("FEE_PAID_MISMATCH", key)
        o.fees_accrued["SETTLEMENT_ADJUSTMENT"] = o.fees_accrued.get("SETTLEMENT_ADJUSTMENT", Decimal(0)) + (assessed - accrued)
        self.cash -= cash_change
        self.fee_payables[key] = assessed - (paid_before + cash_change)
        if self.fee_payables[key] == 0:
            del self.fee_payables[key]

    # ---------------------------------------------------------------- 税批次
    def _consume_lots(self, pos: Position, qty: int) -> list:
        lots = sorted([l for l in pos.lots if l["remaining_qty"] > 0],
                      key=lambda l: (l["acquisition_tax_date"] or "9999", l["seq"], l["lot_id"]))
        left, out = qty, []
        for lot in lots:
            if left == 0:
                break
            take = min(left, lot["remaining_qty"])
            lot["remaining_qty"] -= take
            out.append((lot, take))
            left -= take
        if left:
            raise ContractError("LOT_SHORTFALL", pos.security_id)
        pos.lots = [l for l in pos.lots if l["remaining_qty"] > 0]
        return out

    def _dispose_tax(self, pos: Position, disposals: list, delivery: str, trade_key: str,
                     slices: dict | None = None, payables: dict | None = None, limitations: dict | None = None) -> None:
        """对该批所有尚有补税关系的分红事件同步消耗相同数量；只差额进入应付/应退。
        slices/payables/limitations 默认为账本本身；预览时传入副本。"""
        slices = self.slices if slices is None else slices
        payables = self.tax_payables if payables is None else payables
        limitations = self.limitations if limitations is None else limitations
        groups: dict = {}
        for lot, take in disposals:
            for s in slices.values():
                if s["security_id"] != pos.security_id or s["lot_id"] != lot["lot_id"] or s["unresolved_qty"] <= 0:
                    continue
                use = min(take, s["unresolved_qty"])
                # 已预扣部分按数量比例分摊（到分，余数规则）
                w = money(s["withheld_unapplied"])
                if w:
                    alloc = allocate_cents(w, [("a", Fraction(use)), ("b", Fraction(s["unresolved_qty"] - use))])
                    w_use = alloc["a"]
                else:
                    w_use = Decimal(0)
                s["unresolved_qty"] -= use
                s["withheld_unapplied"] = dec_str(w - w_use)
                if s.get("tax_unknown") or s["selected_rule_id"] is None or lot["acquisition_tax_date"] is None:
                    s["tax_unknown"] = True
                    limitations[f"TAX_UNKNOWN:{s['slice_id']}"] = "TAX_UNKNOWN"
                    continue
                rule = self.fees.tax.by_id(s["selected_rule_id"])
                rate = rule.final_rate(parse_date(lot["acquisition_tax_date"]), parse_date(delivery))
                theo = use * money(s["tax_base_per_share"]) * rate
                gkey = {"ENTITLEMENT": s["action_id"], "LOT": lot["lot_id"]}.get(rule.rounding_unit, trade_key)
                groups.setdefault(gkey, []).append((s, theo, w_use, use))
        for gkey, items in groups.items():
            total = cents(sum((t for _, t, _, _ in items), Decimal(0)))
            alloc = allocate_cents(total, [(s["slice_id"], Fraction(t) if t else Fraction(0)) for s, t, _, _ in items]) \
                if any(t for _, t, _, _ in items) else {s["slice_id"]: Decimal(0) for s, _, _, _ in items}
            for s, theo, w_use, use in items:
                final = alloc[s["slice_id"]]
                extra = final - w_use
                s["disposals"].append({"trade_key": trade_key, "qty": use, "delivery": delivery,
                                       "final_tax": dec_str(final), "withheld_applied": dec_str(w_use),
                                       "additional": dec_str(extra)})
                if extra:
                    payables[s["slice_id"]] = payables.get(s["slice_id"], Decimal(0)) + extra

    def _on_tax_settlement(self, p: dict, ev: dict) -> None:
        sid = p["slice_id"]
        amt = money(p["amount"])
        if self.tax_payables.get(sid, Decimal(0)) != amt:
            raise ContractError("TAX_SETTLEMENT_MISMATCH", sid)
        self.cash -= amt
        del self.tax_payables[sid]

    # ---------------------------------------------------------------- 公司行动
    def _on_ca_record(self, p: dict, ev: dict) -> None:
        """登记日收盘（已处理当日全部已确认成交）锁定真实归属股份。"""
        aid, sid = p["action_id"], p["security_id"]
        if aid in self.entitlements:
            raise ContractError("ACTION_PHASE_REPEATED", f"{aid}/RECORD")
        pos = self.positions.get(sid)
        eligible = pos.quantity if pos else 0
        ent = {"action_id": aid, "security_id": sid, "kind": p["kind"], "record_date": p["record_date"],
               "eligible_qty": eligible, "phases": ["RECORD"], "terms": copy.deepcopy(p),
               "market": pos.market if pos else None, "board": pos.board if pos else None,
               "industry": pos.industry if pos else None}
        self.entitlements[aid] = ent
        if eligible == 0:
            return
        ent["record_lots"] = [(l["lot_id"], l["remaining_qty"]) for l in pos.lots if l["remaining_qty"] > 0]
        ent["record_lot_dates"] = {l["lot_id"]: l["acquisition_tax_date"] for l in pos.lots if l["remaining_qty"] > 0}
        taxable_stock = p["kind"] == "STOCK_DIVIDEND" and p.get("tax_treatment") == "TAXABLE"
        if p["kind"] == "STOCK_DIVIDEND" and p.get("tax_treatment") not in ("TAXABLE", "NOT_TAXABLE"):
            # 送红股/转增是否计税须由事件条款与 TaxProfile 给出；未知不得默认为免税
            self.limitations[f"TAX_UNKNOWN:{aid}"] = "STOCK_DIVIDEND_TAX_TREATMENT_UNKNOWN"
        if p["kind"] == "CASH_DIVIDEND" or taxable_stock:
            rule_id, unknown = None, False
            if self.fees is not None:
                try:
                    rule_id = self.fees.tax.select(parse_date(p["record_date"]), p.get("security_class", "INDIVIDUAL_A_SHARE"),
                                                   p.get("dividend_type", "CASH")).rule_id
                except ContractError as e:
                    unknown = True
                    self.limitations[f"TAX_UNKNOWN:{aid}"] = e.code
            else:
                unknown = True
                self.limitations[f"TAX_UNKNOWN:{aid}"] = "FEE_PROFILE_MISSING"
            ent["binding"] = {"entitlement_id": f"{aid}/{self.account_id}",
                              "tax_profile_id": self.fees.fee_profile_id if self.fees else None,
                              "selected_rule_id": rule_id, "record_date": p["record_date"],
                              "rule_selection_date": p["record_date"], "selection_date_basis": "EQUITY_RECORD_DATE"}
            for lot in pos.lots:
                if lot["remaining_qty"] <= 0:
                    continue
                sl = f"{aid}/{lot['lot_id']}"
                self.slices[sl] = {"slice_id": sl, "action_id": aid, "security_id": sid, "lot_id": lot["lot_id"],
                                   "record_date": p["record_date"], "selected_rule_id": rule_id,
                                   "tax_unknown": unknown or p.get("tax_base_per_share") is None,
                                   "eligible_qty": lot["remaining_qty"], "unresolved_qty": lot["remaining_qty"],
                                   "gross_per_share": p.get("cash_per_share", "0"),
                                   "tax_base_per_share": p.get("tax_base_per_share") or p.get("cash_per_share", "0"),
                                   "withheld_unapplied": "0", "withheld_paid": "0", "disposals": []}

    @staticmethod
    def _entitlement_done(e: dict) -> bool:
        need = {"CASH_DIVIDEND": {"EX", "PAY"}, "STOCK_DIVIDEND": {"EX", "SHARE_CREDIT"}}.get(e["kind"], {"EX"})
        return need <= set(e["phases"])

    def _ent_position(self, ent: dict) -> Position:
        """登记后已卖出、持仓记录被清理时，按登记时的市场/板块恢复空持仓以承接除权/到账。"""
        sid = ent["security_id"]
        if sid not in self.positions:
            self.positions[sid] = Position(sid, ent.get("market", ""), ent.get("board", ""), ent.get("industry"))
        return self.positions[sid]

    def _phase(self, aid: str, phase: str) -> dict:
        ent = self.entitlements.get(aid)
        if ent is None:
            raise ContractError("ACTION_RECORD_MISSING", aid)
        if phase in ent["phases"]:
            raise ContractError("ACTION_PHASE_REPEATED", f"{aid}/{phase}")
        ent["phases"].append(phase)
        return ent

    def _on_ca_ex(self, p: dict, ev: dict) -> None:
        ent = self._phase(p["action_id"], "EX")
        t = ent["terms"]
        if ent["eligible_qty"] == 0:
            return
        pos = self._ent_position(ent)
        if ent["kind"] == "CASH_DIVIDEND":
            amt = money(p["cash_amount"]) if p.get("cash_amount") is not None else \
                cents(ent["eligible_qty"] * money(t["cash_per_share"]))
            ent["receivable"] = dec_str(amt)
            self.receivables[f"DIV:{ent['action_id']}"] = amt
        elif ent["kind"] == "STOCK_DIVIDEND":
            total = Fraction(ent["eligible_qty"]) * ratio(t["shares_per_share"])
            if total.denominator == 1:
                n = int(total)
            elif t.get("rounding") == "FLOOR_ODD_LOT_LOTTERY":
                # 中登规则：零碎股按尾数排序依次发放，单个账户可能得 0 或 1 股；保守按向下取整、不补偿，记录观察
                n = total.numerator // total.denominator
                ent["fraction_dropped"] = str(total - n)
            elif t.get("rounding") == "FLOOR_CASH_IN_LIEU":
                n = total.numerator // total.denominator
                cil = p.get("cash_in_lieu_per_share", t.get("cash_in_lieu_per_share"))
                if cil is not None:
                    frac_cash = cents(Decimal(total.numerator % total.denominator) / Decimal(total.denominator)
                                      * money(cil))
                    self.receivables[f"FRAC:{ent['action_id']}"] = frac_cash
                else:
                    # 有零碎权利但补偿单价未知：不静默丢弃，也不自创金额
                    self.limitations[f"CA_TERMS_UNKNOWN:{ent['action_id']}"] = "CASH_IN_LIEU_PRICE_UNKNOWN"
            else:
                pos.share_receivable_unknown = True
                ent["share_entitlement"] = None
                self.limitations[f"CA_TERMS_UNKNOWN:{ent['action_id']}"] = "FRACTIONAL_ROUNDING_UNKNOWN"
                return
            ent["share_entitlement"] = n
            pos.share_receivable += n
        if pos.quantity + pos.share_receivable == 0:
            return                               # 已全部卖出：只保留应收/应得关系，不需要估值
        if p.get("reference_price") is not None:
            pos.mark = money(p["reference_price"])
            pos.mark_status = "PROVISIONAL_REFERENCE"
            pos.mark_date = p.get("ex_date")
        else:
            # 只有旧股基价格：不得直接旧价×新股数，也不得与应收重复计算
            pos.mark_status = "UNRELIABLE"

    def _on_ca_pay(self, p: dict, ev: dict) -> None:
        ent = self._phase(p["action_id"], "PAY")
        if ent["eligible_qty"] == 0:
            return
        key = f"DIV:{ent['action_id']}"
        gross = self.receivables.pop(key, Decimal(0))
        withheld = money(p.get("withheld", "0"))
        self.cash += gross - withheld
        ent["withheld"] = dec_str(withheld)
        if withheld:
            sl = [s for s in self.slices.values() if s["action_id"] == ent["action_id"]]
            alloc = allocate_cents(withheld, [(s["slice_id"], Fraction(s["eligible_qty"])) for s in sl])
            for s in sl:
                w = alloc[s["slice_id"]]
                s["withheld_paid"] = dec_str(w)
                disposed = s["eligible_qty"] - s["unresolved_qty"]
                if disposed and s["unresolved_qty"]:
                    parts = allocate_cents(w, [("d", Fraction(disposed)), ("u", Fraction(s["unresolved_qty"]))])
                elif disposed:
                    parts = {"d": w, "u": Decimal(0)}
                else:
                    parts = {"d": Decimal(0), "u": w}
                s["withheld_unapplied"] = dec_str(money(s["withheld_unapplied"]) + parts["u"])
                if parts["d"] and s.get("tax_unknown"):
                    # 已转让部分最终税未知：预扣照实作为已付税，不能冲减不存在的应付（否则虚增应退）
                    self.limitations[f"TAX_UNKNOWN:{s['slice_id']}"] = "WITHHELD_ON_UNKNOWN_FINAL_TAX"
                elif parts["d"]:
                    # 已转让部分的最终税已计提：本次预扣用于冲减该应付，不是第二次税负
                    self.tax_payables[s["slice_id"]] = self.tax_payables.get(s["slice_id"], Decimal(0)) - parts["d"]
                    if self.tax_payables[s["slice_id"]] == 0:
                        del self.tax_payables[s["slice_id"]]

    def _on_ca_share_credit(self, p: dict, ev: dict) -> None:
        ent = self._phase(p["action_id"], "SHARE_CREDIT")
        n = ent.get("share_entitlement")
        if not n:
            return
        pos = self._ent_position(ent)
        pos.share_receivable -= n
        pos.quantity += n
        listing = p["sellable_from"]
        pos.pending.append([listing, n])
        # 先按账户整体官方整数，再按批次比例分配，不按 lot 单独四舍五入
        # 按登记时的批次权益分配（登记后卖出不改变应得关系）
        rec_lots = ent.get("record_lots") or [(l["lot_id"], l["remaining_qty"]) for l in pos.lots if l["remaining_qty"] > 0]
        weights = [(lid, Fraction(q)) for lid, q in rec_lots if q > 0]
        alloc = allocate_int(n, weights) if weights else {}
        inherit = p.get("acquisition_inheritance") == "INHERIT_PARENT"
        lot_dates = ent.get("record_lot_dates", {})
        for lot_id, k in alloc.items():
            if k == 0:
                continue
            parent_date = lot_dates.get(lot_id)
            if parent_date is None:
                parent = next((l for l in pos.lots if l["lot_id"] == lot_id), None)
                parent_date = parent["acquisition_tax_date"] if parent else None
            self.lot_seq += 1
            pos.lots.append({"lot_id": f"{pos.security_id}/L{self.lot_seq}", "acquisition_trade_id": ent["action_id"],
                             "acquisition_tax_date": parent_date if inherit else None,
                             "seq": self.lot_seq, "original_qty": k, "remaining_qty": k, "economic_cost": "0",
                             "parent_lot_id": lot_id})
            if not inherit:
                self.limitations[f"TAX_UNKNOWN:{pos.security_id}/L{self.lot_seq}"] = "ACQUISITION_DATE_UNKNOWN"

    def _on_ca_cash_compensation(self, p: dict, ev: dict) -> None:
        """配股权利失效补偿、零碎股现金等真实到账。"""
        ent = self._phase(p["action_id"], p.get("phase", "COMPENSATION"))
        amt = money(p["amount"])
        self.receivables.pop(f"FRAC:{ent['action_id']}", None)
        self.cash += amt

    def _on_ca_conversion(self, p: dict, ev: dict) -> None:
        """换股/合并：按条款转换股数或现金；被动获得的权益继承原归属。"""
        src, dst = p["from_security_id"], p["to_security_id"]
        pos = self.positions.get(src)
        if pos is None or pos.quantity == 0:
            return
        total = Fraction(pos.quantity) * ratio(p["shares_per_share"])
        if total.denominator != 1 and p.get("rounding") != "FLOOR_CASH_IN_LIEU":
            self.limitations[f"CA_TERMS_UNKNOWN:{p['action_id']}"] = "FRACTIONAL_ROUNDING_UNKNOWN"
            raise ContractError("CA_TERMS_UNKNOWN", p["action_id"])
        n = total.numerator // total.denominator
        newpos = self.positions.setdefault(dst, Position(dst, p["to_market"], p["to_board"], p.get("to_industry")))
        newpos.quantity += n
        newpos.pending.append([p["sellable_from"], n])
        newpos.mark = money(p["reference_price"]) if p.get("reference_price") else None
        newpos.mark_status = "PROVISIONAL_REFERENCE" if newpos.mark is not None else "UNRELIABLE"
        for lot in pos.lots:
            lot = dict(lot)
            lot["parent_lot_id"] = lot["lot_id"]
            self.lot_seq += 1
            lot["lot_id"] = f"{dst}/L{self.lot_seq}"
            lot["remaining_qty"] = int(Fraction(lot["remaining_qty"]) * ratio(p["shares_per_share"]))
            lot["original_qty"] = lot["remaining_qty"]
            newpos.lots.append(lot)
        if p.get("cash_per_share"):
            self.cash += cents(pos.quantity * money(p["cash_per_share"]))
        pos.quantity = 0
        pos.lots = []
        if dst in self.etf_ids:
            self.limitations[f"SCOPE_OVERLAP:{dst}"] = "被动换股破坏证券互斥；保留权益、暂停共享新增、人工修正范围"

    def _on_lifecycle(self, p: dict, ev: dict) -> None:
        """已确认终止正常交易：转 RESIDUAL，仅改变正常槽位；E与风险保留，无现金增加。"""
        pos = self.positions[p["security_id"]]
        if p["lifecycle"] != "RESIDUAL":
            raise ContractError("LIFECYCLE_UNSUPPORTED", p["lifecycle"])
        if not p.get("evidence_ref"):
            raise ContractError("LIFECYCLE_EVIDENCE_REQUIRED", p["security_id"])
        pos.lifecycle = "RESIDUAL"

    def _on_residual_recovery(self, p: dict, ev: dict) -> None:
        pos = self.positions[p["security_id"]]
        q = shares(p["qty"])
        if q > pos.quantity:
            raise ContractError("RESIDUAL_QTY_INVALID", p["security_id"])
        self._consume_lots(pos, q)
        pos.quantity -= q
        self.cash += money(p["cash"])

    def _on_position_bootstrap(self, p: dict, ev: dict) -> None:
        """前向模式：人工核对后的初始已有持仓（含税批次）；缺税批次不得假设新购日。"""
        for x in p["positions"]:
            pos = self.positions.setdefault(x["security_id"], Position(x["security_id"], x["market"], x["board"],
                                                                       x.get("industry")))
            pos.quantity += shares(x["quantity"])
            for lot in x.get("lots", []):
                self.lot_seq += 1
                pos.lots.append({"lot_id": f"{x['security_id']}/L{self.lot_seq}", "acquisition_trade_id": lot["ref"],
                                 "acquisition_tax_date": lot.get("acquisition_tax_date"), "seq": self.lot_seq,
                                 "original_qty": lot["qty"], "remaining_qty": lot["qty"],
                                 "economic_cost": lot.get("cost", "0"), "parent_lot_id": None})
            if sum(l["qty"] for l in x.get("lots", [])) != pos.quantity:
                self.limitations[f"TAX_LOTS_INCOMPLETE:{x['security_id']}"] = "BOOTSTRAP"

    def _on_bootstrap_account(self, p: dict, ev: dict) -> None:
        """Bootstrap：人工核对的初始账户（现金+持仓+估值）；初始份额=可信权益，NAV=HWM=1。"""
        if self.capital_started:
            raise ContractError("BOOTSTRAP_AFTER_START")
        self.cash = money(p["cash"])
        for x in p["positions"]:
            pos = Position(x["security_id"], x["market"], x["board"], x.get("industry"))
            pos.quantity = shares(x["quantity"])
            pos.mark, pos.mark_date, pos.mark_status = money(x["mark"]), x.get("mark_date"), "NORMAL"
            self.lot_seq += 1
            pos.lots.append({"lot_id": f"{pos.security_id}/L{self.lot_seq}", "acquisition_trade_id": "BOOTSTRAP",
                             "acquisition_tax_date": x.get("acquisition_tax_date"), "seq": self.lot_seq,
                             "original_qty": pos.quantity, "remaining_qty": pos.quantity,
                             "economic_cost": x.get("cost", "0"), "parent_lot_id": None})
            self.positions[pos.security_id] = pos
        e, st = self.equity()
        if st == "UNKNOWN" or e <= 0:
            raise ContractError("BOOTSTRAP_EQUITY_INVALID")
        self.units, self.hwm, self.last_nav = e, Fraction(1), Fraction(1)
        self.capital_started = True
        self.capital_revision += 1

    def _on_cash_restriction(self, p: dict, ev: dict) -> None:
        """券商/人工确认的非订单现金限制（不改变净资产，只限制购买力）；amount=0 表示解除。"""
        if money(p["amount"]) == 0:
            self.restrictions.pop(p["restriction_id"], None)
        else:
            self.restrictions[p["restriction_id"]] = {"amount": p["amount"], "until": p.get("until"),
                                                      "reason": p["reason"]}

    def _on_share_freeze(self, p: dict, ev: dict) -> None:
        pos = self.positions[p["security_id"]]
        pos.nonorder_frozen = shares(p["qty"])
        pos.nonorder_frozen_reflected = bool(p["reflected_in_broker_can_sell"])

    def _on_broker_position_view(self, p: dict, ev: dict) -> None:
        for sid, v in p["can_sell"].items():
            if sid in self.positions:
                self.positions[sid].broker_can_sell = v

    # ---------------------------------------------------------------- 决策与状态记录
    def _on_risk_episode_start(self, p: dict, ev: dict) -> None:
        e = p["episode"]
        if e["episode_id"] in self.episodes:
            raise ContractError("EPISODE_EXISTS", e["episode_id"])
        self.episodes[e["episode_id"]] = copy.deepcopy(e)

    def _on_risk_episode_end(self, p: dict, ev: dict) -> None:
        e = self.episodes[p["episode_id"]]
        if e["type"] == "EMERGENCY_EXIT" and p.get("reason") != "PROVEN_FALSE_TRIGGER":
            raise ContractError("EMERGENCY_CANNOT_END", "应急真实触发不可自然解除")
        e["active"] = False
        e["fulfilled_at"] = p["at"]
        e["end_reason"] = p["reason"]

    def _on_stress_block(self, p: dict, ev: dict) -> None:
        self.stress_blocks.append(copy.deepcopy(p["block"]))

    def _on_retirement(self, p: dict, ev: dict) -> None:
        if not self.retirement_triggered:
            self.retirement_triggered = True
            self.retirement_trigger_at = p["at"]

    def _on_retirement_correction(self, p: dict, ev: dict) -> None:
        """仅数据错误误触发：证据核正、对账复验后人工撤销；已发生交易不倒删。"""
        for k in ("evidence_ref", "reconciled", "manual_decision_ref"):
            if not p.get(k):
                raise ContractError("CORRECTION_EVIDENCE_REQUIRED", k)
        self.retirement_manually_cleared.append({"at": ev.get("occurred_at"), "was": self.retirement_trigger_at,
                                                 "evidence_ref": p["evidence_ref"]})
        self.retirement_triggered = False
        self.retirement_trigger_at = None
        self.run_state = p.get("restore_state", "RESTRICTED")
        for e in self.episodes.values():
            if e["type"] == "EMERGENCY_EXIT" and e["active"]:
                e["active"] = False
                e["end_reason"] = "PROVEN_FALSE_TRIGGER"

    def _on_run_state(self, p: dict, ev: dict) -> None:
        to = p["to"]
        if self.retirement_triggered and to in ("NORMAL", "RESTRICTED", "WAITING_START"):
            raise ContractError("RETIRED_TRIGGERED", "真实停用标记不可自动恢复")
        if self.run_state == "RETIRED" and to != "RETIRED":
            raise ContractError("RETIRED_TRIGGERED")
        self.run_state = to

    def _on_plan(self, p: dict, ev: dict) -> None:
        self.plans[p["plan"]["plan_id"]] = p["plan"]
        self.current_plan_id = p["plan"]["plan_id"]
        # 只保留最近若干计划，旧计划已写入日志
        if len(self.plans) > 8:
            for k in list(self.plans)[:-8]:
                del self.plans[k]

    def _on_counters(self, p: dict, ev: dict) -> None:
        if p["decision_id"] in self.counter_decisions:
            raise ContractError("COUNTER_DECISION_REPEATED", p["decision_id"])
        self.counter_decisions.append(p["decision_id"])
        self.counters = dict(p["counts"])

    def _on_week_stat(self, p: dict, ev: dict) -> None:
        self.week_stats.append(dict(p))

    def _on_cursor(self, p: dict, ev: dict) -> None:
        self.cursor = dict(p)

    def _on_run_binding(self, p: dict, ev: dict) -> None:
        """一次运行只绑定一个规范化身份；不同内容不得续写同一账本（H08.3 内容比较，不做哈希体系）。"""
        if self.binding is not None and canon(self.binding) != canon(p["binding"]):
            raise ContractError("STUDY_DEFINITION_MISMATCH", "账本已绑定不同的研究/输入内容")
        self.binding = copy.deepcopy(p["binding"])

    def _on_limitation(self, p: dict, ev: dict) -> None:
        if p.get("clear"):
            self.limitations.pop(p["code"], None)
        else:
            self.limitations[p["code"]] = p.get("detail", "")

    def _on_decision_log(self, p: dict, ev: dict) -> None:
        self.decision_log.append(p)
        if len(self.decision_log) > 200:
            self.decision_log = self.decision_log[-200:]

    # ================================================================ 快照
    def snapshot(self) -> dict:
        def frac(x):
            return wire_ratio(x) if isinstance(x, Fraction) else x
        e, _ = self.equity()
        state = {
            "cash": dec_str(self.cash), "restrictions": self.restrictions,
            "receivables": {k: dec_str(v) for k, v in self.receivables.items()},
            "fee_payables": {k: dec_str(v) for k, v in self.fee_payables.items()},
            "tax_payables": {k: dec_str(v) for k, v in self.tax_payables.items()},
            "positions": {k: v.to_json() for k, v in self.positions.items()},
            "orders": {k: v.to_json() for k, v in self.orders.items()},
            "entitlements": self.entitlements, "slices": self.slices,
            "units": wire_ratio(self.units), "hwm": wire_ratio(self.hwm), "last_nav": wire_ratio(self.last_nav),
            "capital_started": self.capital_started, "capital_terminated": self.capital_terminated,
            "capital_revision": self.capital_revision, "capital_flows": self.capital_flows,
            "capacity_used": {k: dec_str(v) for k, v in self.capacity_used.items()},
            "ledger_points": [{k: (frac(v) if not isinstance(v, Decimal) else dec_str(v)) for k, v in pt.items()}
                              for pt in self.ledger_points],
            "run_state": self.run_state, "retirement_triggered": self.retirement_triggered,
            "retirement_trigger_at": self.retirement_trigger_at,
            "retirement_manually_cleared": self.retirement_manually_cleared,
            "episodes": self.episodes, "stress_blocks": self.stress_blocks, "counters": self.counters,
            "counter_decisions": self.counter_decisions, "plans": self.plans, "current_plan_id": self.current_plan_id,
            "week_stats": self.week_stats, "cursor": self.cursor, "limitations": self.limitations,
            "seen": self.seen, "trade_log": self.trade_log, "decision_log": self.decision_log, "lot_seq": self.lot_seq,
            "binding": self.binding,
        }
        exact = {"detail_version": DETAIL_VERSION, "projection_revision": self.projection_revision,
                 "last_committed_seq": self.seq, "equity": None, "units": wire_ratio(self.units),
                 "nav": None, "hwm": wire_ratio(self.hwm), "dd": None}
        if self.units > 0:
            from .contracts import frac_exact_decimal
            ed = frac_exact_decimal(e)
            exact["equity"] = dec_str(ed) if ed is not None else None
            nav = e / self.units
            exact["nav"] = wire_ratio(nav)
            exact["dd"] = wire_ratio(1 - nav / self.hwm)
        return {"detail_version": DETAIL_VERSION, "account_id": self.account_id, "seq": self.seq,
                "revision": self.revision, "projection_revision": self.projection_revision, "exact": exact,
                "state": state}

    @classmethod
    def restore(cls, snap: dict, fee_schedule: FeeSchedule | None = None, etf_ids=()) -> "Ledger":
        if snap.get("detail_version") != DETAIL_VERSION:
            raise ContractError("EXACT_STATE_REPLAY_REQUIRED", "非V5D2精确快照，须由可信事件重建")
        s = snap["state"]
        lg = cls(snap["account_id"], fee_schedule, etf_ids)
        lg.cash = money(s["cash"])
        lg.restrictions = s["restrictions"]
        lg.receivables = {k: money(v) for k, v in s["receivables"].items()}
        lg.fee_payables = {k: money(v) for k, v in s["fee_payables"].items()}
        lg.tax_payables = {k: money(v) for k, v in s["tax_payables"].items()}
        lg.positions = {k: Position.from_json(v) for k, v in s["positions"].items()}
        lg.orders = {k: Order.from_json(v) for k, v in s["orders"].items()}
        lg._open_ids = {k for k, o in lg.orders.items() if o.open}
        lg.entitlements, lg.slices = s["entitlements"], s["slices"]
        lg.units, lg.hwm, lg.last_nav = unwire_ratio(s["units"]), unwire_ratio(s["hwm"]), unwire_ratio(s["last_nav"])
        lg.capital_started, lg.capital_terminated = s["capital_started"], s["capital_terminated"]
        lg.capital_revision, lg.capital_flows = s["capital_revision"], s["capital_flows"]
        lg.capacity_used = {k: money(v) for k, v in s["capacity_used"].items()}
        pts = []
        for pt in s["ledger_points"]:
            q = {}
            for k, v in pt.items():
                if isinstance(v, dict) and set(v) == {"num", "den"}:
                    q[k] = unwire_ratio(v)
                elif k in ("cash", "receivables", "liabilities") and v is not None:
                    q[k] = money(v)
                else:
                    q[k] = v
            pts.append(q)
        lg.ledger_points = pts
        for k in ("run_state", "retirement_triggered", "retirement_trigger_at", "retirement_manually_cleared",
                  "episodes", "stress_blocks", "counters", "counter_decisions", "plans", "current_plan_id",
                  "week_stats", "cursor", "limitations", "seen", "trade_log", "decision_log", "lot_seq", "binding"):
            setattr(lg, k, s[k])
        lg.seq, lg.revision, lg.projection_revision = snap["seq"], snap["revision"], snap["projection_revision"]
        # 精确缓存必须与从资产重算的精确比例一致
        ex = snap["exact"]
        if unwire_ratio(ex["units"]) != lg.units or unwire_ratio(ex["hwm"]) != lg.hwm:
            raise ContractError("EXACT_CACHE_MISMATCH", "units/hwm")
        if lg.capital_started and lg.hwm < 1:
            raise ContractError("EXACT_CACHE_MISMATCH", "hwm<1")
        if lg.units > 0:
            e, _ = lg.equity()
            nav = e / lg.units
            if unwire_ratio(ex["nav"]) != nav or unwire_ratio(ex["dd"]) != 1 - nav / lg.hwm:
                raise ContractError("EXACT_CACHE_MISMATCH", "nav/dd")
            if ex.get("equity") is not None and ratio(ex["equity"]) != e:
                raise ContractError("EXACT_CACHE_MISMATCH", "equity")
        return lg


def check_exact_snapshot(ex: dict) -> dict:
    """ExactAccountSnapshot 业务校验：规范约分、NAV=E/U、DD=1−NAV/HWM、HWM>0。非日终切片DD可为负。"""
    if ex.get("detail_version") != DETAIL_VERSION:
        raise ContractError("EXACT_STATE_REPLAY_REQUIRED", str(ex.get("detail_version")))
    u, nav, hwm, dd = (unwire_ratio(ex[k]) for k in ("units", "nav", "hwm", "dd"))
    e = ratio(ex["equity"])
    if u < 0 or hwm <= 0:
        raise ContractError("EXACT_CACHE_MISMATCH", "units/hwm sign")
    if u == 0 or e / u != nav or 1 - nav / hwm != dd:
        raise ContractError("EXACT_CACHE_MISMATCH", "nav/dd")
    return {"equity": e, "units": u, "nav": nav, "hwm": hwm, "dd": dd}


def dd_flags(nav: Fraction, hwm: Fraction) -> dict:
    """正式日终：先更新HWM再计算非负DD；整数交叉乘等价比较。"""
    h = max(hwm, nav)
    dd = 1 - nav / h
    return {"hwm": h, "dd": dd, "warning": dd >= DD_WARNING, "retire": dd >= DD_RETIRE, "mdd_pass": dd <= DD_LIMIT}


def reproject(account_id: str, facts: list[dict], revision: str, fee_schedule: FeeSchedule | None = None,
              checkpoint: dict | None = None, etf_ids=()) -> tuple["Ledger", dict]:
    """H07.4 晚到会计纠正：不可变事实（资本流金额/时间、真实成交、来源原文）按经济生效顺序重新投影派生量
    （份额、流前NAV、HWM/DD）。不同 projection_revision 的旧快照一律失效，从起点重建；不倒造订单。
    facts 每条带 effective_key（可排序）；同 event_id 重复同内容幂等，异内容冲突。"""
    uniq: dict = {}
    for i, f in enumerate(facts):
        if f["event_id"] in uniq:
            old = uniq[f["event_id"]][1]
            if canon({"k": old["kind"], "p": old["payload"]}) != canon({"k": f["kind"], "p": f["payload"]}):
                raise ContractError("FACT_CONFLICT", f["event_id"])
            continue
        uniq[f["event_id"]] = (i, f)
    ordered = [f for _, f in sorted(uniq.values(), key=lambda x: (x[1]["effective_key"], x[0]))]
    rejected = False
    if checkpoint is not None and checkpoint.get("projection_revision") != revision:
        checkpoint, rejected = None, True
    if checkpoint is not None:
        lg = Ledger.restore(checkpoint, fee_schedule, etf_ids)
    else:
        lg = Ledger(account_id, fee_schedule, etf_ids)
        lg.projection_revision = revision
    for f in ordered:
        lg.apply(f)
    return lg, {"checkpoint_rejected": rejected, "applied_order": [f["event_id"] for f in ordered]}
