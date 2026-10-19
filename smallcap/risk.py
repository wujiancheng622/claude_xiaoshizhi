"""风险段、运行状态、两级禁买裁决与合并卖量（D07.4—7.5、D08、H06.2）。

风险模块是证券级与全局禁买的唯一裁决者；执行器只消费 RiskDecision，不添加
“存在任何风险段就全局禁买”的隐含规则。合并卖量取最严格剩余目标，不相加。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction

from .contracts import (BLOCKED_EXIT_TRIGGER, DD_RETIRE, DD_WARNING, INDUSTRY_LIMIT, MIN_RAW_CLOSE, SINGLE_TARGET,
                        SINGLE_TRIGGER, STOCK_TARGET, STOCK_TRIGGER, wire_ratio)
from .rules import QuantityGrid

REDUCE_TYPES = ("SINGLE_REDUCE", "INDUSTRY_REDUCE", "TOTAL_REDUCE", "EMERGENCY_EXIT")
PRIORITY = {"EMERGENCY_EXIT": 0, "SECURITY_EXIT": 1, "SINGLE_REDUCE": 2, "INDUSTRY_REDUCE": 3, "TOTAL_REDUCE": 4,
            "ORDINARY_EXIT": 5, "ORDINARY_REDUCE": 5, "ORDINARY_BUY": 6}


def new_episode(etype: str, key: str, seq: int, trigger_at: str, first_eligible: str, scope_members: list,
                target_ratio: Fraction | None, detail: str = "") -> dict:
    return {"episode_id": f"{etype}:{key}:{seq}", "type": etype, "key": key, "trigger_at": trigger_at,
            "scope_members": sorted(scope_members), "target_ratio": wire_ratio(target_ratio) if target_ratio is not None else None,
            "active": True, "first_eligible_session": first_eligible, "stress_end": None, "fulfilled_at": None,
            "detail": detail}


def active(episodes: dict, etype: str | None = None) -> list[dict]:
    return [e for e in episodes.values() if e["active"] and (etype is None or e["type"] == etype)]


# ---------------------------------------------------------------- 日终检测
@dataclass
class RiskView:
    """一致估值时点的风险输入。values 含冻结、残余与股份应收。"""
    equity: Fraction
    values: dict                  # sid -> Fraction
    industry_of: dict             # sid -> industry（None 为未知）
    quantities: dict              # sid -> int
    closes: dict = field(default_factory=dict)        # sid -> 已核实收盘原价
    risk_facts: dict = field(default_factory=dict)    # sid -> ST/DELISTING_ARRANGEMENT/TERMINATED/MV_FLOOR_FAIL
    dd: Fraction | None = None


def detect(view: RiskView, episodes: dict, *, at: str, first_eligible: str, seq: int) -> tuple[list, list, dict]:
    """返回 (新段列表, 结束段列表[(id, reason)], 标志)。同一段每日重算不生成新 episode_id。"""
    starts, ends = [], []
    flags = {"warning": False, "retire": False}
    E = view.equity
    stock = sum(view.values.values(), Fraction(0))
    act = {(e["type"], e["key"]): e for e in active(episodes)}
    # 应急：日终 DD≥25%
    if view.dd is not None:
        flags["warning"] = view.dd >= DD_WARNING
        if view.dd >= DD_RETIRE:
            flags["retire"] = True
            if not any(e["type"] == "EMERGENCY_EXIT" for e in episodes.values()):
                starts.append(new_episode("EMERGENCY_EXIT", "ACCOUNT", seq, at, first_eligible,
                                          [s for s, q in view.quantities.items() if q > 0], None, "DD>=25%"))
    # 证券确定退出：ST/退市整理/终止上市/价格<2/市值底线失效（目标为0，与E无关，资不抵债时也照常检测）
    for sid, q in view.quantities.items():
        if q <= 0:
            continue
        why = view.risk_facts.get(sid)
        c = view.closes.get(sid)
        if why is None and c is not None and c < MIN_RAW_CLOSE:
            why = "PRICE_BELOW_2"
        if why and ("SECURITY_EXIT", sid) not in act:
            starts.append(new_episode("SECURITY_EXIT", sid, seq, at, first_eligible, [sid], Fraction(0), why))
    for (t, key), e in act.items():
        if t == "SECURITY_EXIT" and view.quantities.get(key, 0) == 0:
            ends.append((e["episode_id"], "DISPOSED"))
    if E <= 0:
        return starts, ends, flags
    # 单股
    for sid, v in view.values.items():
        e = act.get(("SINGLE_REDUCE", sid))
        if e is None and v > SINGLE_TRIGGER * E:
            starts.append(new_episode("SINGLE_REDUCE", sid, seq, at, first_eligible, [sid], SINGLE_TARGET))
        elif e is not None and v <= SINGLE_TARGET * E:
            ends.append((e["episode_id"], "TARGET_SATISFIED"))
    for (t, key), e in act.items():
        if t == "SINGLE_REDUCE" and key not in view.values:
            ends.append((e["episode_id"], "TARGET_SATISFIED"))
    # 行业（未知行业不删出集中度；由全局限制处理）
    ind: dict = {}
    for sid, v in view.values.items():
        ind.setdefault(view.industry_of.get(sid), []).append(sid)
    for k, sids in ind.items():
        if k is None:
            continue
        v = sum((view.values[s] for s in sids), Fraction(0))
        e = act.get(("INDUSTRY_REDUCE", k))
        if e is None and v > INDUSTRY_LIMIT * E:
            starts.append(new_episode("INDUSTRY_REDUCE", k, seq, at, first_eligible, sids, INDUSTRY_LIMIT))
        elif e is not None and v <= INDUSTRY_LIMIT * E:
            ends.append((e["episode_id"], "TARGET_SATISFIED"))
    for (t, key), e in act.items():
        if t == "INDUSTRY_REDUCE" and key not in ind:
            ends.append((e["episode_id"], "TARGET_SATISFIED"))
    # 总暴露：>65%触发，≤60%结束
    e = act.get(("TOTAL_REDUCE", "ACCOUNT"))
    if e is None and stock > STOCK_TRIGGER * E:
        starts.append(new_episode("TOTAL_REDUCE", "ACCOUNT", seq, at, first_eligible,
                                  [s for s in view.values], STOCK_TARGET))
    elif e is not None and stock <= STOCK_TARGET * E:
        ends.append((e["episode_id"], "TARGET_SATISFIED"))
    return starts, ends, flags


def satisfied_now(episode: dict, view: RiskView) -> bool:
    """以当前实际股数计算（不扣未成交卖量）是否已自然满足。"""
    E = view.equity
    t = episode["type"]
    if t == "SINGLE_REDUCE":
        return view.values.get(episode["key"], Fraction(0)) <= SINGLE_TARGET * E
    if t == "INDUSTRY_REDUCE":
        v = sum((x for s, x in view.values.items() if view.industry_of.get(s) == episode["key"]), Fraction(0))
        return v <= INDUSTRY_LIMIT * E
    if t == "TOTAL_REDUCE":
        return sum(view.values.values(), Fraction(0)) <= STOCK_TARGET * E
    if t == "SECURITY_EXIT":
        return view.quantities.get(episode["key"], 0) == 0
    return False


# ---------------------------------------------------------------- 两级禁买裁决
def risk_decision(*, account_revision: int, episodes: dict, run_state: str, equity: Fraction | None,
                  valuation_status: str, account_limitations: dict, unknown_industry_holdings: list,
                  order_state_unknown: bool, batch_blocks: list, security_exits: dict, no_buy_in_batch: set,
                  hold_no_add: set, risk_sell_sids: set, blocked_exit_value: Fraction,
                  quote_revision: str = "") -> dict:
    """唯一的两级禁买裁决。global 非空则全部新买关闭；否则只禁对应证券。"""
    g: set = set()
    for e in active(episodes):
        if e["type"] in REDUCE_TYPES:
            g.add(e["type"])
    if run_state in ("EXITING", "RETIRED"):
        g.add("RUN_STATE_" + run_state)
    if equity is not None and equity <= 0:
        g.add("INSOLVENT")
    if valuation_status == "UNKNOWN" or equity is None:
        g.add("VALUATION_UNKNOWN")
    if unknown_industry_holdings:
        g.add("INDUSTRY_UNKNOWN")
    if order_state_unknown:
        g.add("ORDER_STATE_UNKNOWN")
    for code in account_limitations:
        base = code.split(":")[0]
        if base in ("EVENT_CONFLICT", "CASH_RECONCILIATION_FAILED", "POSITION_MISMATCH", "FEE_UNKNOWN", "SCOPE_OVERLAP",
                    "CONFIRMED_EXECUTION_BREACH", "ORDER_RECONCILE", "CA_TERMS_UNKNOWN", "EXACT_STATE_REPLAY_REQUIRED",
                    "CAPITAL_FLOW_POLICY_BREACH", "STUDY_DEFINITION_MISMATCH"):
            g.add(base)
    g.update(batch_blocks)
    if equity is not None and equity > 0 and blocked_exit_value >= BLOCKED_EXIT_TRIGGER * equity:
        g.add("BLOCKED_EXIT_EXPOSURE")
    sec: dict = {}
    for sid, why in security_exits.items():
        sec.setdefault(sid, set()).add("SECURITY_EXIT")
    for sid in no_buy_in_batch:
        sec.setdefault(sid, set()).add("SOLD_IN_BATCH")
    for sid in hold_no_add:
        sec.setdefault(sid, set()).add("HOLD_NO_ADD")
    for sid in risk_sell_sids:
        sec.setdefault(sid, set()).add("RISK_SELL_TARGET")
    return {"account_revision": account_revision, "quote_revision": quote_revision, "run_state": run_state,
            "global_buy_blocks": sorted(g), "security_buy_blocks": {k: sorted(v) for k, v in sorted(sec.items())}}


def final_intents(*, plan: dict | None, decision: dict, episodes: dict, sell: dict, account_revision: int,
                  quote_basis: str, valid_window: str, cap_reference_date: str) -> list[dict]:
    """H06.2 FinalIntent：只表达经济目标（不含订单数量取整）；prohibit_buy 为风险裁决与普通计划禁买的并集。"""
    modes = (plan or {}).get("modes", {})
    acts = active(episodes)
    sids = sorted(set(modes) | set(sell) | {e["key"] for e in acts if e["type"] in ("SECURITY_EXIT", "SINGLE_REDUCE")})
    out = []
    for sid in sids:
        md = modes.get(sid)
        eps = [e for e in acts if e["key"] == sid or sid in e["scope_members"]
               or e["type"] in ("TOTAL_REDUCE", "EMERGENCY_EXIT")]
        if eps:
            prio = min(PRIORITY[e["type"]] for e in eps)
        elif md is not None and md["mode"] == "EXIT":
            prio = PRIORITY["ORDINARY_EXIT"]
        elif md is not None and md["mode"] == "TARGET_AMOUNT" and md.get("reason") == "NEW_BUY":
            prio = PRIORITY["ORDINARY_BUY"]
        else:
            prio = PRIORITY["ORDINARY_REDUCE"]
        reasons = sorted(set(decision["global_buy_blocks"]) | set(decision["security_buy_blocks"].get(sid, [])))
        out.append({"security_id": sid, "ordinary_mode": md["mode"] if md else None,
                    "ordinary_amount_or_quantity": (md.get("target_amount") if md and md["mode"] in ("TARGET_AMOUNT", "EXIT")
                                                    else (md.get("locked_shares") if md else None)),
                    "max_remaining_shares_or_amount": {"sell_shares": sell.get(sid, 0)},
                    "risk_episode_ids": sorted(e["episode_id"] for e in eps), "priority": prio,
                    "prohibit_buy": bool(reasons) or bool(md and md.get("prohibit_buy")),
                    "valid_window": valid_window, "cap_reference_date": cap_reference_date,
                    "reason_codes": reasons + ([md["reason"]] if md else []),
                    "account_revision": account_revision, "quote_basis": quote_basis,
                    "plan_id": (plan or {}).get("plan_id")})
    return out


def buy_allowed(decision: dict, sid: str) -> bool:
    return not decision["global_buy_blocks"] and sid not in decision["security_buy_blocks"]


# ---------------------------------------------------------------- 合并风险卖量
@dataclass
class SellLeg:
    sid: str
    q: int                        # 实际股数（估值数量含股份应收另计在 value 中）
    m: Fraction                   # 估值价
    p: Decimal                    # 预期卖价（保护价/代理价）
    industry: str | None
    free_sell: int                # D07.3 free_sell（当前可执行；受阻则为0）
    grid: QuantityGrid
    cap_qty: int                  # 容量允许的最大卖量
    value: Fraction               # 该证券全部估值（含冻结、股份应收）


class SellPlanner:
    """按优先级合并卖量；所有目标用合并后数量、只计一次的费用重新检查（费用闭合）。"""

    def __init__(self, equity: Fraction, legs: dict[str, SellLeg], fee_fn, tax_fn=None):
        self.E = equity
        self.legs = legs
        self.fee_fn = fee_fn          # (sid, qty, price) -> Decimal 合并委托费用
        self.tax_fn = tax_fn or (lambda sid, qty: Decimal(0))   # (sid, qty) -> 新增股息差别税；None=未知
        self.X: dict[str, int] = {s: 0 for s in legs}
        self.unreachable: dict[str, Fraction] = {}
        self.tax_unknown: set = set()

    # -- 账户预览
    def e_after(self, X=None) -> Fraction:
        X = self.X if X is None else X
        e = self.E
        for s, x in X.items():
            if x:
                leg = self.legs[s]
                tax = self.tax_fn(s, x)
                if tax is None:
                    # 税未知不得按0证明目标已满足：记录后该证券的风险目标只能报告为未证实
                    self.tax_unknown.add(s)
                    tax = Decimal(0)
                e += x * (Fraction(leg.p) - leg.m) - Fraction(self.fee_fn(s, x, leg.p)) - Fraction(tax)
        return e

    def v_after(self, s: str, X=None) -> Fraction:
        X = self.X if X is None else X
        leg = self.legs[s]
        return leg.value - X.get(s, 0) * leg.m

    def violated(self, target: tuple, X=None) -> bool:
        X = self.X if X is None else X
        kind, key = target
        e = self.e_after(X)
        if kind == "SINGLE":
            return self.v_after(key, X) > SINGLE_TARGET * e
        if kind == "INDUSTRY":
            return sum((self.v_after(s, X) for s, l in self.legs.items() if l.industry == key), Fraction(0)) > INDUSTRY_LIMIT * e
        if kind == "TOTAL":
            return sum((self.v_after(s, X) for s in self.legs), Fraction(0)) > STOCK_TARGET * e
        if kind == "EXIT":
            return self.X.get(key, 0) < self.legs[key].q
        return False

    def legal_cap(self, s: str) -> int:
        leg = self.legs[s]
        return leg.grid.max_sell(min(leg.free_sell, leg.cap_qty), leg.free_sell, leg.q)

    # -- 目标
    def exit_all(self, s: str) -> None:
        self.X[s] = max(self.X[s], self.legal_cap(s))
        if self.X[s] < self.legs[s].q:
            self.unreachable[f"EXIT:{s}"] = Fraction(self.legs[s].q - self.X[s])

    def _advance_until(self, target: tuple, members: list[str]) -> bool:
        """在成员内按可卖市值同比例推进合法数量档，直至目标满足；返回是否满足。"""
        if not self.violated(target):
            return True
        heap = []
        seqs = {}
        for s in sorted(members):
            leg = self.legs[s]
            base = self.X[s]
            a = leg.free_sell - base
            if a <= 0:
                continue
            seq = leg.grid.sell_sequence(base, leg.free_sell, leg.q, leg.cap_qty)
            if not seq:
                continue
            seqs[s] = (base, seq, a)
            heapq.heappush(heap, (Fraction(seq[0], a), s, 0))
        while heap:
            r0 = heap[0][0]
            group = []
            while heap and heap[0][0] == r0:
                group.append(heapq.heappop(heap))
            for _, s, i in group:
                base, seq, a = seqs[s]
                self.X[s] = base + seq[i]
                if i + 1 < len(seq):
                    heapq.heappush(heap, (Fraction(seq[i + 1], a), s, i + 1))
            if not self.violated(target):
                return True
        return False

    def run(self, *, exits: list[str], singles: list[str], industries: list[str], total: bool,
            ordinary: dict[str, int] | None = None, max_loops: int = 50) -> dict:
        for s in exits:
            if s in self.legs:
                self.exit_all(s)
        targets = [("SINGLE", s) for s in singles if s in self.legs]
        targets += [("INDUSTRY", k) for k in sorted(industries)]
        if total:
            targets.append(("TOTAL", "ACCOUNT"))
        for _ in range(max_loops):
            before = dict(self.X)
            for t in targets:
                members = [t[1]] if t[0] == "SINGLE" else (
                    [s for s, l in self.legs.items() if l.industry == t[1]] if t[0] == "INDUSTRY" else list(self.legs))
                self._advance_until(t, members)
            # 费用闭合复核：全部合并卖量与只计一次的费用下仍须满足
            if all(not self.violated(t) for t in targets) or self.X == before:
                break
        for t in targets:
            if self.violated(t):
                self.unreachable[f"{t[0]}:{t[1]}"] = Fraction(1)
            elif self.tax_unknown:
                self.unreachable[f"TAX_UNKNOWN:{t[0]}:{t[1]}"] = Fraction(1)
        if ordinary:
            for s, x in ordinary.items():
                if s in self.legs and x > self.X[s]:
                    self.X[s] = x
        e_after = self.e_after()
        return {"sell": {s: x for s, x in self.X.items() if x > 0}, "unreachable": sorted(self.unreachable),
                "e_after": e_after, "tax_unknown": sorted(self.tax_unknown)}


def blocked_exit_value(required: dict[str, int], executable: dict[str, int], marks: dict) -> Fraction:
    """应风险退出而当前确实不能执行的剩余股数×估值，证券去重。"""
    tot = Fraction(0)
    for sid, need in required.items():
        blocked = max(need - executable.get(sid, 0), 0)
        if blocked and marks.get(sid) is not None:
            tot += blocked * Fraction(marks[sid])
    return tot


def open_order_cancellations(ledger, needed_sell: dict | None, buy_block_reason: str | None = None,
                             decision: dict | None = None, plan_capital_revision: int | None = None) -> list:
    """D09.4：先更新真实账户，再把未结订单与最新意图比较；返回 [(client_order_id, cancel_reason)]。
    needed_sell 为以当前实际股数计算的合并所需卖量（不先扣该单未成交卖量）；None 表示本次不比较卖单。
    已发 cancel_required 在终态前不撤回，也不重复发。"""
    out = []
    for o in ledger.open_orders():
        if o.needs_followup_cancel(ledger.revision):
            out.append((o.client_order_id, "FOLLOWUP_AFTER_CANCEL_FAILED"))
            continue
        if o.cancel_required:
            continue
        if o.side == "SELL" and needed_sell is not None:
            need = needed_sell.get(o.security_id, 0)
            if need == 0:
                out.append((o.client_order_id, "TARGET_SATISFIED"))
            elif need < o.remaining_possible:
                out.append((o.client_order_id, "TARGET_SHRUNK"))
        elif o.side == "BUY":
            if plan_capital_revision is not None and o.kind == "ORDINARY" and \
                    ledger.capital_revision != o.capital_revision:
                out.append((o.client_order_id, "CAPITAL_CHANGED"))
            elif decision is not None and decision["global_buy_blocks"]:
                out.append((o.client_order_id, "GLOBAL_BUY_BLOCK:" + decision["global_buy_blocks"][0]))
            elif decision is not None and o.security_id in decision["security_buy_blocks"]:
                out.append((o.client_order_id, "SECURITY_BUY_BLOCK"))
            elif buy_block_reason:
                out.append((o.client_order_id, buy_block_reason))
    return sorted(out)
