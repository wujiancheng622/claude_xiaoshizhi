"""两策略人工串行批次、现金承诺、原子转资与双视图对账（D14、V5 §10）。

这是人工维护并确认的资金台账与串行批次的最小实现，不是已验收的自动协调模块；
未经外部演练（G18-07/08）前，只能输出计划与清单，不能启用共享实盘。
"""
from __future__ import annotations

import copy
from decimal import Decimal

from .contracts import ContractError, Result, OK, INVALID, UNKNOWN, money, reason

OWNERS = ("ETF", "STOCK")
IDLE, AUTHORIZED, EXECUTING, RECONCILING = "IDLE", "AUTHORIZED", "EXECUTING", "RECONCILING"
PREAPPROVED, BROKER_FROZEN, CONSUMED, RELEASED = "PREAPPROVED", "BROKER_FROZEN", "CONSUMED", "RELEASED"


class SharedCash:
    def __init__(self, etf_cash: Decimal, stock_cash: Decimal, unassigned: Decimal = Decimal(0)):
        self.C = {"ETF": money(etf_cash), "STOCK": money(stock_cash), "UNASSIGNED": money(unassigned)}
        self.restricted = {o: Decimal(0) for o in OWNERS}          # 非订单原因的已确认限制
        self.due = {o: Decimal(0) for o in OWNERS}                 # 已知待付且未在他处预留
        self.due_reflected = {o: Decimal(0) for o in OWNERS}       # 其中已体现在券商可用里的部分
        self.tax_reserve = {o: Decimal(0) for o in OWNERS}
        self.commitments: dict[str, dict] = {}
        self.state = IDLE
        self.batch: dict | None = None
        self.revision = 0
        self.unknown_orders: dict[str, set] = {o: set() for o in OWNERS}
        self.discrepancies: list = []
        self.events: list = []
        self.seen_transfers: set = set()
        self.broker_available: Decimal | None = None
        self.broker_snapshot_revision = -1

    # ---------------------------------------------------------------- 事实
    def _bump(self, kind: str, **payload) -> None:
        self.revision += 1
        self.events.append({"revision": self.revision, "kind": kind, **copy.deepcopy(payload)})

    def broker_snapshot(self, available: Decimal) -> int:
        """券商已确认可用现金（已扣其冻结）；返回快照绑定的修订号。"""
        self.broker_available = money(available)
        self._bump("BROKER_SNAPSHOT", available=str(available))
        self.broker_snapshot_revision = self.revision
        return self.revision

    def outstanding(self, owner: str) -> Decimal:
        return sum((c["amount"] for c in self.commitments.values()
                    if c["owner"] == owner and c["state"] in (PREAPPROVED, BROKER_FROZEN)), Decimal(0))

    def own_free(self, owner: str) -> Decimal:
        """own_free = own_cash_book − own_restricted − own_outstanding_commitments − due − contingent_tax_reserve。"""
        return (self.C[owner] - self.restricted[owner] - self.outstanding(owner) - self.due[owner]
                - self.tax_reserve[owner])

    def broker_free(self) -> Decimal | None:
        """broker_free = broker_confirmed_available − 已批准未体现在券商冻结的占用 − 未体现的待付款。"""
        if self.broker_available is None:
            return None
        approved_not_frozen = sum((c["amount"] for c in self.commitments.values() if c["state"] == PREAPPROVED),
                                  Decimal(0))
        due_not_reflected = sum((self.due[o] - self.due_reflected[o] for o in OWNERS), Decimal(0))
        return self.broker_available - approved_not_frozen - due_not_reflected

    def batch_limit(self, owner: str) -> Decimal | None:
        b = self.broker_free()
        return None if b is None else min(self.own_free(owner), b)

    # ---------------------------------------------------------------- 串行批次
    def authorize(self, owner: str, batch_id: str, amount: Decimal, snapshot_revision: int,
                  increases_risk: bool = True) -> Result:
        amount = money(amount)
        why = []
        if self.state != IDLE:
            why.append(reason("SERIAL_BATCH_BUSY", "BATCH", batch_id, f"当前{self.state}"))
        if snapshot_revision != self.revision:
            why.append(reason("STALE_SNAPSHOT", "BATCH", batch_id, "批准须依据不早于最近成交/转资/冻结的快照"))
        if increases_risk and any(self.unknown_orders[o] for o in OWNERS):
            why.append(reason("ORDER_STATE_UNKNOWN", "BATCH", batch_id))
        if increases_risk and self.discrepancies:
            why.append(reason("CASH_RECONCILIATION_FAILED", "BATCH", batch_id))
        lim = self.batch_limit(owner)
        if lim is None:
            why.append(reason("BROKER_CASH_UNKNOWN", "BATCH", batch_id))
        elif amount > lim:
            why.append(reason("INSUFFICIENT_CASH", "BATCH", batch_id, f"{amount}>{lim}"))
        if why:
            return Result(INVALID, None, why)
        self.state = AUTHORIZED
        self.batch = {"owner": owner, "batch_id": batch_id, "amount": amount, "used": Decimal(0),
                      "account_revision": snapshot_revision, "orders_sent": 0}
        self._bump("AUTHORIZE", owner=owner, batch_id=batch_id, amount=str(amount))
        return Result(OK, self.batch)

    def revoke_unstarted(self, owner: str) -> bool:
        """另一方应急优先：只能撤回尚未发单的批次；已发委托须先确认/撤销。"""
        if self.batch and self.batch["owner"] == owner and self.batch["orders_sent"] == 0:
            self._bump("REVOKE", batch_id=self.batch["batch_id"])
            self.batch, self.state = None, IDLE
            return True
        return False

    def request_priority(self, owner: str) -> Result:
        """股票应急退出优先于尚未启动的另一方普通买入批次。"""
        if self.state == IDLE:
            return Result(OK, "IDLE")
        other = self.batch["owner"]
        if other == owner:
            return Result(OK, "OWN_BATCH")
        if self.revoke_unstarted(other):
            return Result(OK, "REVOKED_UNSTARTED_OTHER")
        return Result(UNKNOWN, "WAIT_OTHER_ORDERS", [reason("OTHER_BATCH_ORDERS_IN_FLIGHT", "BATCH", self.batch["batch_id"],
                                                          "先确认/撤销对方在途委托，不假装不存在")])

    def preapprove(self, commitment_id: str, owner: str, amount: Decimal, order_id: str) -> None:
        if self.state not in (AUTHORIZED, EXECUTING) or self.batch["owner"] != owner:
            raise ContractError("NOT_AUTHORIZED", commitment_id)
        amount = money(amount)
        if self.batch["used"] + amount > self.batch["amount"]:
            raise ContractError("AUTHORIZATION_EXCEEDED", commitment_id)
        self.batch["used"] += amount
        self.commitments[commitment_id] = {"owner": owner, "batch_id": self.batch["batch_id"], "amount": amount,
                                           "state": PREAPPROVED, "order_id": order_id}
        self._bump("PREAPPROVE", commitment_id=commitment_id, amount=str(amount))

    def broker_frozen(self, commitment_id: str, broker_available_after: Decimal) -> None:
        """PREAPPROVED → BROKER_FROZEN：只转移状态，不重复扣两次，也不在转移中释放。"""
        c = self.commitments[commitment_id]
        if c["state"] != PREAPPROVED:
            raise ContractError("COMMITMENT_STATE", commitment_id)
        c["state"] = BROKER_FROZEN
        self.broker_available = money(broker_available_after)
        self.state = EXECUTING
        self.batch["orders_sent"] += 1
        self._bump("BROKER_FROZEN", commitment_id=commitment_id)

    def fill(self, commitment_id: str, cash_out: Decimal, released_freeze: Decimal | None = None) -> None:
        """部分成交转为真实现金支出，剩余冻结继续。"""
        c = self.commitments[commitment_id]
        cash_out = money(cash_out)
        self.C[c["owner"]] -= cash_out
        c["amount"] -= min(cash_out, c["amount"])
        if c["amount"] == 0:
            c["state"] = CONSUMED
        self._bump("FILL", commitment_id=commitment_id, cash_out=str(cash_out))

    def cancel_confirmed(self, commitment_id: str) -> None:
        """撤单确认才释放未成交部分；撤单请求不释放。"""
        c = self.commitments[commitment_id]
        c["state"] = RELEASED
        self._bump("RELEASE", commitment_id=commitment_id, amount=str(c["amount"]))
        c["amount"] = Decimal(0)

    def mark_unknown(self, owner: str, order_id: str, unknown: bool = True) -> None:
        (self.unknown_orders[owner].add if unknown else self.unknown_orders[owner].discard)(order_id)

    def end_batch(self) -> Result:
        if self.state not in (AUTHORIZED, EXECUTING):
            raise ContractError("NO_ACTIVE_BATCH")
        self.state = RECONCILING
        open_c = [k for k, c in self.commitments.items() if c["state"] in (PREAPPROVED, BROKER_FROZEN)
                  and c["batch_id"] == self.batch["batch_id"]]
        if open_c or any(self.unknown_orders[o] for o in OWNERS) or self.discrepancies:
            return Result(UNKNOWN, self.state, [reason("BATCH_NOT_RECONCILED", "BATCH", self.batch["batch_id"],
                                                       f"open={open_c}")])
        self._bump("BATCH_END", batch_id=self.batch["batch_id"])
        self.state, self.batch = IDLE, None
        return Result(OK, IDLE)

    # ---------------------------------------------------------------- 归属与转资
    def credit_proceeds(self, owner: str, amount: Decimal) -> None:
        """卖出回款先归卖出方，不自动成为另一策略的买入资金。"""
        self.C[owner] += money(amount)
        self._bump("PROCEEDS", owner=owner, amount=str(amount))

    def transfer(self, transfer_id: str, src: str, dst: str, amount: Decimal) -> Result:
        """只转已确认、无占用、覆盖待付需要的现金；单条共同事件 ΔC_A+ΔC_B=0；重复同ID幂等。"""
        amount = money(amount)
        if transfer_id in self.seen_transfers:
            return Result(OK, "DUPLICATE")
        if amount <= 0:
            return Result(INVALID, None, [reason("TRANSFER_INVALID", "TRANSFER", transfer_id)])
        if amount > self.own_free(src):
            return Result(INVALID, None, [reason("TRANSFER_EXCEEDS_FREE_CASH", "TRANSFER", transfer_id,
                                                 "不得动用冻结/承诺/待付/待税现金")])
        if self.state != IDLE:
            return Result(INVALID, None, [reason("SERIAL_BATCH_BUSY", "TRANSFER", transfer_id)])
        self.C[src] -= amount
        self.C[dst] += amount
        self.seen_transfers.add(transfer_id)
        self._bump("TRANSFER", transfer_id=transfer_id, src=src, dst=dst, amount=str(amount))
        return Result(OK, {"transfer_id": transfer_id, "flows": {src: str(-amount), dst: str(amount)}})

    # ---------------------------------------------------------------- 对账
    def reconcile(self, broker_cash_book: Decimal, explained: list[dict] | None = None) -> Result:
        """C_broker_book = C_ETF + C_STOCK + C_UNASSIGNED + 已解释时点差异；未解释差额不塞入任何一方。"""
        explained = explained or []
        exp_total = sum((money(x["amount"]) for x in explained), Decimal(0))
        for x in explained:
            if x.get("kind") == "DISPLAY_ROUNDING" and abs(money(x["amount"])) > Decimal("0.01"):
                return Result(INVALID, None, [reason("ROUNDING_EXPLANATION_TOO_LARGE", "CASH", "", str(x["amount"]))])
        diff = money(broker_cash_book) - (self.C["ETF"] + self.C["STOCK"] + self.C["UNASSIGNED"] + exp_total)
        if diff != 0:
            self.discrepancies.append({"diff": str(diff)})
            return Result(INVALID, {"diff": diff}, [reason("CASH_RECONCILIATION_FAILED", "CASH", "", f"未解释差额 {diff}")])
        self.discrepancies = []
        return Result(OK, {"diff": Decimal(0), "explained": explained})


def check_etf_view(view: dict) -> Result:
    """ETF 侧须按自己的证券和划归权益形成计划；把全券商 total_asset 当自身权益则外部边界不合格。"""
    if view.get("equity_source") == "BROKER_TOTAL_ASSET":
        return Result(INVALID, None, [reason("ETF_VIEW_USES_TOTAL_ASSET", "EXTERNAL", "ETF",
                                             "局部归属快照未补前不启用共享实盘")])
    if view.get("equity_source") != "OWN_ALLOCATED_VIEW":
        return Result(UNKNOWN, None, [reason("ETF_VIEW_UNVERIFIED", "EXTERNAL", "ETF")])
    return Result(OK, None)


def double_count_check(strategy_cash: dict, broker_cash_book: Decimal) -> Result:
    """子策略现金之和不得超过券商记账现金（拒绝双份权益/购买力）。"""
    tot = sum((money(v) for v in strategy_cash.values()), Decimal(0))
    if tot > money(broker_cash_book):
        return Result(INVALID, None, [reason("DOUBLE_COUNTED_CASH", "CASH", "", f"{tot}>{broker_cash_book}")])
    return Result(OK, None)
