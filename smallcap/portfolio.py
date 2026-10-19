"""周度意图与普通组合算法（D06、H06.1）：保留/退出/补位、固定金额、30/45缓冲、FLAT_SCORE。

只依据截止时已核账账户、池、评分与计数形成计划；不越过现金、风险、名额和执行限制（由执行与风险裁决）。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .contracts import (B2_RETAIN_RANK, BAND_HIGH, BAND_LOW, CENTER, INDUSTRY_LIMIT, RETAIN_RANK, SLOTS,
                        wire_ratio)
from .factors import update_missing_counters

HOLD_SHARES, HOLD_NO_ADD, EXIT, TARGET_AMOUNT = "HOLD_SHARES", "HOLD_NO_ADD", "EXIT", "TARGET_AMOUNT"


@dataclass
class HoldingView:
    security_id: str
    quantity: int
    value: Fraction                 # 截止时估值
    industry: str | None
    lifecycle: str = "NORMAL"
    frozen: bool = False            # 当前已知不能卖（停牌/冻结/无可卖量）
    open_sell: bool = False
    risk_exit: bool = False         # SECURITY_EXIT / EMERGENCY 作用中（从普通流程隔离）
    confirmed_ineligible: bool = False


def retain_limit(role: str) -> int:
    return B2_RETAIN_RANK if role == "B2" else RETAIN_RANK


def plan_week(*, role: str, plan_id: str, cutoff: str, e_decision: Fraction, holdings: list[HoldingView],
              open_buy_sids: set, pool: dict, score: dict | None, counters: dict, capital_revision: int,
              execute_session: str | None, eligibility: dict | None = None) -> tuple[dict, dict]:
    """返回 (WeeklyPlan, 新缺失计数)。role: B0（市值）/B1（因子+45）/B2（因子+30）。"""
    retain = retain_limit(role)
    modes: dict[str, dict] = {}
    skipped: list = []
    held = [h for h in holdings if h.quantity > 0]
    normal = [h for h in held if h.lifecycle == "NORMAL" and not h.risk_exit]
    new_counters = dict(counters)
    batch_healthy = bool(score and score["healthy"])
    flat = bool(score and score["flat"] and batch_healthy and role != "B0")
    if role != "B0" and score is not None:
        new_counters, cact = update_missing_counters(counters, [h.security_id for h in normal], batch_healthy,
                                                     score, pool)
    else:
        cact = {}
    target = CENTER * e_decision
    eligibility = eligibility or pool.get("eligibility", {})

    def keep_mode(h: HoldingView, prohibit_reason: str | None = None):
        w = h.value / e_decision if e_decision > 0 else None
        if flat:
            if w is not None and w > BAND_HIGH:
                return {"mode": TARGET_AMOUNT, "target_amount": wire_ratio(target), "prohibit_buy": True,
                        "reason": "FLAT_SCORE_REDUCE_ONLY"}
            return {"mode": HOLD_SHARES, "target_amount": None, "prohibit_buy": True, "reason": "FLAT_SCORE"}
        if w is not None and BAND_LOW <= w <= BAND_HIGH:
            return {"mode": HOLD_SHARES, "target_amount": None, "prohibit_buy": False, "reason": "IN_BAND"}
        return {"mode": TARGET_AMOUNT, "target_amount": wire_ratio(target), "prohibit_buy": False,
                "reason": "OUT_OF_BAND"}

    for h in normal:
        sid = h.security_id
        confirmed_bad = h.confirmed_ineligible or eligibility.get(sid) == "FALSE"
        if not batch_healthy:
            # 全局不健康：暂停普通排名及调权；只保留确定资格退出（风险另行处理）
            modes[sid] = ({"mode": EXIT, "target_amount": "0", "prohibit_buy": True, "reason": "CONFIRMED_INELIGIBLE"}
                          if confirmed_bad else
                          {"mode": HOLD_NO_ADD, "target_amount": None, "prohibit_buy": True,
                           "reason": (score or {}).get("reason") or "BATCH_UNHEALTHY"})
            continue
        if confirmed_bad:
            modes[sid] = {"mode": EXIT, "target_amount": "0", "prohibit_buy": True, "reason": "CONFIRMED_INELIGIBLE"}
            continue
        if sid not in pool["size_order"]:
            if pool["health"] == "OK":
                modes[sid] = {"mode": EXIT, "target_amount": "0", "prohibit_buy": True, "reason": "OFF_POOL"}
            else:
                modes[sid] = {"mode": HOLD_NO_ADD, "target_amount": None, "prohibit_buy": True,
                              "reason": "POOL_MEMBERSHIP_UNKNOWN"}
            continue
        if role == "B0":
            r = score["ranks"][sid]
            modes[sid] = keep_mode(h) if r <= retain else {"mode": EXIT, "target_amount": "0", "prohibit_buy": True,
                                                            "reason": f"RANK_{r}_GT_{retain}"}
            modes[sid]["rank"] = r
            continue
        a = cact.get(sid, "RANK")
        if a == "RANK":
            r = score["ranks"][sid]
            if flat or r <= retain:
                modes[sid] = keep_mode(h)
            else:
                modes[sid] = {"mode": EXIT, "target_amount": "0", "prohibit_buy": True, "reason": f"RANK_{r}_GT_{retain}"}
            modes[sid]["rank"] = r
        elif a == "EXIT_MODEL":
            modes[sid] = {"mode": EXIT, "target_amount": "0", "prohibit_buy": True, "reason": "MODEL_INAPPLICABLE"}
        elif a == "EXIT_PERSISTENT":
            modes[sid] = {"mode": EXIT, "target_amount": "0", "prohibit_buy": True, "reason": "PERSISTENT_UNSCORABLE"}
        else:
            modes[sid] = {"mode": HOLD_NO_ADD, "target_amount": None, "prohibit_buy": True,
                          "reason": "TEMPORARILY_UNSCORABLE"}

    # 槽位：保留仓、HOLD_NO_ADD、冻结或卖不掉的退出、风险退出仓、未结买入并集计一次
    occupied = set()
    for h in held:
        if h.lifecycle != "NORMAL":
            continue
        m = modes.get(h.security_id)
        if h.risk_exit or m is None:
            if h.frozen or h.open_sell or not h.risk_exit:
                occupied.add(h.security_id)
            continue
        if m["mode"] == EXIT and not h.frozen and not h.open_sell:
            continue                   # 名单层假设未被冻结、无未结束卖单的计划退出完成
        occupied.add(h.security_id)
    occupied |= set(open_buy_sids)
    free_slots = max(SLOTS - len(occupied), 0)
    # 被动换股等使正常证券超过30：只记录并禁增，不按代码顺序删权益（D06.2）
    slots_exceeded = len(occupied) > SLOTS

    buys: list[str] = []
    ordinary_buy_allowed = batch_healthy and not flat and not slots_exceeded
    plan_reason = None if ordinary_buy_allowed else (
        "FLAT_SCORE" if flat else ((score or {}).get("reason") or "NO_SCORE") if not batch_healthy else "SLOTS_EXCEEDED")
    if ordinary_buy_allowed and free_slots > 0:
        # 行业规划：规划后保留市值（TARGET_AMOUNT 按固定中心金额，未冻结且无在途卖单的退出按0）+ 新股 0.02E
        ind_val: dict = {}
        for h in held:
            m = modes.get(h.security_id)
            if m is not None and m["mode"] == EXIT and not h.frozen and not h.open_sell:
                continue
            planned = target if (m is not None and m["mode"] == TARGET_AMOUNT and not h.frozen) else h.value
            ind_val[h.industry] = ind_val.get(h.industry, Fraction(0)) + planned
        held_ids = {h.security_id for h in held} | set(open_buy_sids)
        industries = pool.get("industries", {})
        for sid in score["order"]:
            if len(buys) >= free_slots:
                break
            if sid in held_ids:
                continue
            ind = industries.get(sid)
            if ind is None:
                skipped.append({"security_id": sid, "reason": "INDUSTRY_UNKNOWN"})
                continue
            if ind_val.get(ind, Fraction(0)) + target > INDUSTRY_LIMIT * e_decision:
                skipped.append({"security_id": sid, "reason": "INDUSTRY_LIMIT"})
                continue
            buys.append(sid)
            ind_val[ind] = ind_val.get(ind, Fraction(0)) + target
            modes[sid] = {"mode": TARGET_AMOUNT, "target_amount": wire_ratio(target), "prohibit_buy": False,
                          "reason": "NEW_BUY", "rank": score["ranks"][sid]}
    for h in normal:
        m = modes.get(h.security_id)
        if m is not None and m["mode"] in (HOLD_SHARES, HOLD_NO_ADD):
            m["locked_shares"] = h.quantity       # 持股锁定保存股份基础，不重新定权
    plan = {"plan_id": plan_id, "role": role, "source_cutoff": cutoff, "E_decision": wire_ratio(e_decision),
            "execute_session": execute_session, "capital_revision": capital_revision,
            "slots_exceeded": slots_exceeded,
            "score_status": "FLAT_SCORE" if flat else ("OK" if batch_healthy else "UNHEALTHY"),
            "score_reason": plan_reason, "ordinary_buy_allowed": ordinary_buy_allowed, "modes": modes,
            "buy_candidates_ordered": buys, "free_slots_planned": free_slots, "occupied_slots": sorted(occupied),
            "skipped": skipped, "package_id": (score or {}).get("package_id"),
            "pool_health": pool["health"], "pool_size": pool["size"],
            "coverage": wire_ratio(score["coverage"]) if score and score.get("coverage") is not None else None}
    return plan, new_counters
