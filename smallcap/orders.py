"""委托事实与唯一状态迁移（D09.3、H05.3）。

只保存不可改写的提交身份/原数量/限价、已核实逐笔成交集合、累计量额、终态证据、撤销请求；
状态由事实按“不可变成交 → 最新终态 → 活动撤销请求 → 受理/提交”优先级归约，不是任意手填。
本模块不改变账户；成交的股数/现金/费用由 account.Ledger 在同一原子事件中入账。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .contracts import ContractError, dec_str, money, shares

CREATED, RESERVED, SENT_UNKNOWN, ACKED, PARTIAL = "CREATED", "RESERVED", "SENT_UNKNOWN", "ACKED", "PARTIAL"
CANCEL_PENDING, FILLED, CANCELED, REJECTED = "CANCEL_PENDING", "FILLED", "CANCELED", "REJECTED"
RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
CLOSED = frozenset({FILLED, CANCELED, REJECTED})
ORDER_STATES = (CREATED, RESERVED, SENT_UNKNOWN, ACKED, PARTIAL, CANCEL_PENDING, FILLED, CANCELED, REJECTED,
                RECONCILE_REQUIRED)


@dataclass
class Order:
    client_order_id: str
    batch_id: str
    plan_id: str
    security_id: str
    market: str
    board: str
    side: str                     # BUY / SELL
    qty: int                      # 原申报量，提交后不可改写
    limit: Decimal
    kind: str                     # ORDINARY / RISK
    trade_date: str               # 市场日期 YYYY-MM-DD
    capital_revision: int
    created_at: str
    window_end: str
    attempt: int = 1
    intent_reason: str = ""
    upper_price_bound: str | None = None   # 卖单容量预约用的当日合法成交价上界
    reserved: bool = False
    submitted: bool = False
    submitted_at: str | None = None
    acked: bool = False
    broker_order_id: str | None = None
    local_canceled: bool = False
    fills: dict = field(default_factory=dict)          # trade_key -> {qty, price, amount, occurred_at, received_at}
    filled_qty: int = 0
    filled_value: Decimal = Decimal(0)
    fees_accrued: dict = field(default_factory=dict)   # fee_kind -> Decimal（该委托累计已计提）
    fee_unknown: bool = False
    cancel_required: bool = False
    cancel_reason: str = ""
    cancel_request_id: str | None = None
    cancel_pending: bool = False
    cancel_actions: int = 0
    cancel_failed_revision: int | None = None          # 明确撤单失败且原单仍活动时的账户修订号
    cancel_followup_sent: bool = False                 # 失败后的“一次后续取消”是否已输出
    terminal: dict | None = None                       # {status, cum_qty, cum_value, source_revision}
    control_revision: int = -1
    sticky_conflicts: list = field(default_factory=list)
    breach: bool = False
    observations: list = field(default_factory=list)
    status: str = CREATED

    # ------------------------------------------------------------ 归约
    def derive(self) -> str:
        f, q = self.filled_qty, self.qty
        if self.sticky_conflicts or f > q:
            s = RECONCILE_REQUIRED
        elif self.local_canceled:
            s = CANCELED
        elif self.terminal is not None:
            t = self.terminal
            tq, tv = t["cum_qty"], money(t["cum_value"])
            if tq != f or (tq == f and tv != self.filled_value):
                s = RECONCILE_REQUIRED       # 终态累计与逐笔不一致：补查或冲突
            elif f == q:
                s = FILLED
            elif t["status"] == FILLED:
                s = RECONCILE_REQUIRED       # 声称全成但累计不足原量
            elif t["status"] == REJECTED and f > 0:
                s = RECONCILE_REQUIRED
            else:
                s = t["status"]
        elif f == q:
            s = FILLED
        elif self.cancel_pending:
            s = CANCEL_PENDING
        elif f > 0:
            s = PARTIAL
        elif self.acked:
            s = ACKED
        elif self.submitted:
            s = SENT_UNKNOWN
        elif self.reserved:
            s = RESERVED
        else:
            s = CREATED
        if s in (FILLED,) or (s in CLOSED):
            self.cancel_pending = False
        if s == FILLED:
            self.cancel_required = False
        self.status = s
        return s

    @property
    def closed(self) -> bool:
        return self.status in CLOSED

    @property
    def open(self) -> bool:
        return not self.closed

    @property
    def remaining_possible(self) -> int:
        """仍可能继续成交的数量上界；未知/冲突时保留最大可能剩余。"""
        if self.status in CLOSED:
            return 0
        return max(self.qty - self.filled_qty, 0)

    @property
    def known_unresolved(self) -> bool:
        return self.status in (SENT_UNKNOWN, RECONCILE_REQUIRED, CANCEL_PENDING)

    # ------------------------------------------------------------ 事件
    def reserve(self) -> None:
        if self.status != CREATED:
            raise ContractError("ORDER_TRANSITION_INVALID", f"{self.client_order_id} reserve from {self.status}")
        self.reserved = True
        self.derive()

    def local_cancel(self, reason: str) -> None:
        if self.submitted:
            raise ContractError("ORDER_TRANSITION_INVALID", "已提交订单不能本地直接取消，须请求券商撤单")
        self.local_canceled = True
        self.cancel_reason = reason
        self.derive()

    def submit(self, at: str) -> None:
        if self.status != RESERVED:
            raise ContractError("ORDER_TRANSITION_INVALID", f"{self.client_order_id} submit from {self.status}")
        self.submitted = True
        self.submitted_at = at
        self.derive()

    def ack(self, broker_order_id: str | None) -> None:
        if self.status in CLOSED or self.status == RECONCILE_REQUIRED:
            self.observations.append({"kind": "LATE_ACK", "broker_order_id": broker_order_id})
        if broker_order_id and self.broker_order_id and broker_order_id != self.broker_order_id:
            self.sticky_conflicts.append("BROKER_ID_CONFLICT")
        self.broker_order_id = self.broker_order_id or broker_order_id
        self.acked = True
        self.submitted = True
        self.derive()

    def add_fill(self, trade_key: str, qty: int, price: Decimal, amount: Decimal | None,
                 occurred_at: str, received_at: str) -> str:
        """返回 'NEW' / 'DUPLICATE' / 'CONFLICT'。相同 trade_key 相同内容完全幂等。"""
        qty = shares(qty)
        price = money(price)
        if qty <= 0 or price <= 0:
            raise ContractError("FILL_INVALID", trade_key)
        amt = money(amount) if amount is not None else qty * price
        if amount is not None and amt != qty * price and not trade_key.startswith("CUM:"):
            # 来源已给成交额，须按来源精度核对
            if abs(amt - qty * price) > Decimal("0.01"):
                raise ContractError("FILL_AMOUNT_MISMATCH", trade_key)
        rec = {"qty": qty, "price": dec_str(price), "amount": dec_str(amt), "side": self.side}
        if trade_key in self.fills:
            old = self.fills[trade_key]
            if {k: old[k] for k in rec} == rec:
                return "DUPLICATE"
            self.sticky_conflicts.append(f"TRADE_ID_CONFLICT:{trade_key}")
            self.derive()
            return "CONFLICT"
        rec.update(occurred_at=occurred_at, received_at=received_at)
        self.fills[trade_key] = rec
        self.filled_qty += qty
        self.filled_value += amt
        self.submitted = True
        if self.filled_qty > self.qty:
            self.breach = True
        if self.side == "BUY" and price > self.limit or self.side == "SELL" and price < self.limit:
            self.breach = True
        self.derive()
        return "NEW"

    def request_cancel(self, request_id: str, reason: str) -> bool:
        """返回是否形成新的撤销请求（已有未决撤销不重复发）。"""
        if self.status in CLOSED:
            self.observations.append({"kind": "CANCEL_AFTER_CLOSE", "request_id": request_id})
            return False
        if not self.submitted:
            self.local_cancel(reason)
            return False
        new = not self.cancel_pending
        if new and self.cancel_failed_revision is not None:
            self.cancel_followup_sent = True       # 失败后的后续取消只此一次
        self.cancel_required = True
        self.cancel_reason = self.cancel_reason or reason
        if new:
            self.cancel_pending = True
            self.cancel_request_id = request_id
            self.cancel_actions += 1
        self.derive()
        return new

    def cancel_failed(self, original_still_open: bool | None, revision: int | None = None) -> None:
        if self.status in CLOSED:
            self.observations.append({"kind": "LATE_CANCEL_FAILED"})
            return
        if original_still_open is True:
            self.cancel_pending = False      # 原预约保留，cancel_required 仍为真
            self.cancel_failed_revision = revision
            self.cancel_followup_sent = False
        else:
            self.sticky_conflicts.append("CANCEL_OUTCOME_UNKNOWN")
        self.derive()

    def needs_followup_cancel(self, current_revision: int) -> bool:
        """D09.4：已确认失败且原单仍活动，只在新的可信账户/订单修订下输出一次后续取消动作。"""
        return (self.open and self.cancel_required and not self.cancel_pending and not self.cancel_followup_sent
                and self.cancel_failed_revision is not None and current_revision > self.cancel_failed_revision)

    def terminal_evidence(self, status: str, cum_qty: int, cum_value: Decimal, source_revision: int) -> None:
        if status not in (FILLED, CANCELED, REJECTED):
            raise ContractError("TERMINAL_STATUS_INVALID", status)
        new = {"status": status, "cum_qty": shares(cum_qty), "cum_value": dec_str(money(cum_value)),
               "source_revision": source_revision}
        if source_revision < self.control_revision:
            self.observations.append({"kind": "OLDER_TERMINAL", **new})   # 明确较老版本，只保存观察
            return
        if source_revision == self.control_revision and self.terminal is not None:
            if {k: self.terminal[k] for k in ("status", "cum_qty", "cum_value")} != \
                    {k: new[k] for k in ("status", "cum_qty", "cum_value")}:
                self.sticky_conflicts.append("TERMINAL_VERSION_CONFLICT")
            self.derive()
            return
        self.control_revision = source_revision
        # 全成后相容的迟到“撤销结果/终态”（累计=q）只作观察，不改变 FILLED
        if self.filled_qty == self.qty and new["cum_qty"] == self.qty and money(new["cum_value"]) == self.filled_value:
            self.terminal = dict(new, status=FILLED if new["status"] != REJECTED else REJECTED)
            if new["status"] != FILLED:
                self.observations.append({"kind": "COMPATIBLE_LATE_TERMINAL", **new})
        else:
            self.terminal = new
        self.derive()

    def resolve(self, evidence_ref: str) -> None:
        """人工/查询证据足以唯一解释后清除粘滞冲突；仍按事实归约，不手填状态。"""
        self.observations.append({"kind": "RESOLVED", "evidence_ref": evidence_ref,
                                  "cleared": list(self.sticky_conflicts)})
        self.sticky_conflicts = []
        self.derive()

    # ------------------------------------------------------------ 序列化
    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["limit"] = dec_str(self.limit)
        d["filled_value"] = dec_str(self.filled_value)
        d["fees_accrued"] = {k: dec_str(v) for k, v in self.fees_accrued.items()}
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Order":
        d = dict(d)
        d["limit"] = money(d["limit"])
        d["filled_value"] = money(d["filled_value"])
        d["fees_accrued"] = {k: money(v) for k, v in d["fees_accrued"].items()}
        o = cls(**d)
        status = o.status
        if o.derive() != status:
            raise ContractError("ORDER_CACHE_MISMATCH", o.client_order_id)
        if o.filled_qty != sum(f["qty"] for f in o.fills.values()):
            raise ContractError("ORDER_FILL_MISMATCH", o.client_order_id)
        return o
