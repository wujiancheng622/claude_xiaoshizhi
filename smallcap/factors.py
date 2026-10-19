"""因子包登记、联合有效集、精确平均并列百分位评分、缺失计数与包级可用性（D05、H03）。

主流程无 winsorize、无 Z-score、无市值/行业中性化；百分位用整数精确计算：
并列块占位 a…b → z=(a+b−n−1)/(n−1)；n=1 时 z=0；综合 S=(Σz)/K。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from .contracts import (JOINT_COVERAGE, MAX_BAD_STREAK, PACKAGE_HEALTHY_RATIO, UNKNOWN_HEALTHY_WEEKS,
                        ContractError, money, quantize)

VALID, MODEL_INAPPLICABLE, STALE = "VALID", "MODEL_INAPPLICABLE", "STALE"
SOURCE_UNKNOWN, SOURCE_CONFLICT = "SOURCE_UNKNOWN", "SOURCE_CONFLICT"
FACTOR_STATUSES = (VALID, MODEL_INAPPLICABLE, STALE, SOURCE_UNKNOWN, SOURCE_CONFLICT)


@dataclass(frozen=True)
class FactorValue:
    value: Decimal | None
    status: str
    reason: str = ""
    max_available_at: datetime | None = None


def _test_only_fixture(spec: dict, pool_ids: list[str], cutoff: datetime, facts: dict) -> dict:
    """TEST_ONLY：直接返回用例提供的测试分数；不是投资因子。facts[sid]['fixture_value']=(value|None,status,available_at)"""
    out = {}
    for sid in pool_ids:
        rec = facts.get(sid, {}).get("fixture_value")
        if rec is None:
            out[sid] = FactorValue(None, SOURCE_UNKNOWN, "FIXTURE_MISSING")
            continue
        value, status, avail = rec
        if avail is not None and avail > cutoff:
            out[sid] = FactorValue(None, SOURCE_UNKNOWN, "FUTURE_DEPENDENCY", avail)
            continue
        if status != VALID:
            out[sid] = FactorValue(None, status, "FIXTURE_STATUS", avail)
            continue
        out[sid] = FactorValue(money(value), VALID, "", avail)
    return out


REGISTRY = {"TEST_ONLY_VALUE_FROM_FIXTURE": _test_only_fixture}


def validate_package(pkg: dict) -> None:
    if not pkg.get("factors"):
        raise ContractError("FACTOR_PACKAGE_EMPTY")
    ids = [f["factor_id"] for f in pkg["factors"]]
    if len(ids) != len(set(ids)):
        raise ContractError("FACTOR_ID_DUPLICATE")
    for f in pkg["factors"]:
        if f["direction"] not in ("HIGHER_BETTER", "LOWER_BETTER"):
            raise ContractError("FACTOR_DIRECTION_INVALID", f["factor_id"])
        if f["max_report_age_months"] > 15:
            raise ContractError("FRESHNESS_TOO_LOOSE", f["factor_id"])
        if f["implementation_id"].startswith("TEST_ONLY") and pkg["purpose"] != "TEST_ONLY":
            raise ContractError("TEST_FACTOR_IN_INVESTMENT_PACKAGE", f["factor_id"])
    for k, v in (("aggregation", "EQUAL_WEIGHT"), ("preprocessing", "AVERAGE_TIE_PERCENTILE_MINUS1_PLUS1"),
                 ("joint_rule", "ALL_FACTORS_VALID"), ("neutralization", "NONE")):
        if pkg[k] != v:
            raise ContractError("FACTOR_PACKAGE_RULE", k)


def compute_factor(spec: dict, pool_ids: list[str], cutoff: datetime, facts: dict) -> dict:
    """纯函数：已登记实现名 → 实现；未给公式的实现返回 FACTOR_IMPLEMENTATION_MISSING。"""
    impl = REGISTRY.get(spec["implementation_id"])
    if impl is None:
        raise ContractError("FACTOR_IMPLEMENTATION_MISSING", spec["implementation_id"])
    raw = impl(spec, pool_ids, cutoff, facts)
    q = money(spec["output_quantum"])
    out = {}
    for sid, fv in raw.items():
        if fv.status == VALID:
            out[sid] = FactorValue(quantize(fv.value, q), VALID, fv.reason, fv.max_available_at)
        else:
            out[sid] = fv
    return out


def percentile_scores(values: dict[str, Decimal], direction: str) -> dict[str, Fraction]:
    """平均并列名次映射到[−1,1]；方向只反转一次。"""
    n = len(values)
    if n == 0:
        return {}
    if n == 1:
        return {k: Fraction(0) for k in values}
    sign = 1 if direction == "HIGHER_BETTER" else -1
    keyed = sorted(values.items(), key=lambda kv: sign * kv[1])
    out, i = {}, 0
    while i < n:
        j = i
        v = sign * keyed[i][1]
        while j + 1 < n and sign * keyed[j + 1][1] == v:
            j += 1
        a, b = i + 1, j + 1                  # 名次从1计
        z = Fraction(a + b - n - 1, n - 1)
        for k in range(i, j + 1):
            out[keyed[k][0]] = z
        i = j + 1
    return out


def score_package(pkg: dict, pool: dict, factor_values: dict[str, dict], codes: dict[str, str]) -> dict:
    """factor_values: {factor_id: {sid: FactorValue}}。返回 ScoreResult。"""
    U = list(pool["security_ids"])
    K = len(pkg["factors"])
    J, invalid = [], {}
    for sid in U:
        bad = [(f["factor_id"], factor_values[f["factor_id"]].get(sid, FactorValue(None, SOURCE_UNKNOWN)).status)
               for f in pkg["factors"]]
        bad = [(fid, st) for fid, st in bad if st != VALID]
        if bad:
            invalid[sid] = bad
        else:
            J.append(sid)
    n, u = len(J), len(U)
    coverage = Fraction(n, u) if u else None
    healthy = pool["health"] == "OK" and u > 0 and 5 * n >= 4 * u
    sums = {sid: Fraction(0) for sid in J}
    for f in pkg["factors"]:
        vals = {sid: factor_values[f["factor_id"]][sid].value for sid in J}
        for sid, z in percentile_scores(vals, f["direction"]).items():
            sums[sid] += z
    S = {sid: s / K for sid, s in sums.items()}
    order = sorted(J, key=lambda sid: (-S[sid], codes[sid]))
    flat = len(set(S.values())) <= 1 and n > 0
    return {"package_id": pkg["package_id"], "purpose": pkg["purpose"], "joint_ids": J, "valid_count": n,
            "pool_count": u, "coverage": coverage, "healthy": healthy, "score_fraction": S,
            "ranks": {sid: i + 1 for i, sid in enumerate(order)}, "order": order, "flat": flat,
            "invalid_reasons": invalid,
            "reason": None if healthy else ("EMPTY_ECONOMIC_POOL" if u == 0 else
                                            "POOL_UNCERTAIN" if pool["health"] != "OK" else "JOINT_COVERAGE_LOW")}


def b0_ranking(pool: dict) -> dict:
    """B0：同一经济池内按市值/代码排序，不要求评分因子有效。"""
    order = list(pool["security_ids"])
    return {"package_id": None, "purpose": "B0_SIZE", "joint_ids": order, "valid_count": len(order),
            "pool_count": len(order), "coverage": None, "healthy": pool["health"] == "OK" and len(order) > 0,
            "score_fraction": {}, "ranks": {sid: i + 1 for i, sid in enumerate(order)}, "order": order,
            "flat": False, "invalid_reasons": {},
            "reason": None if pool["health"] == "OK" and order else
            ("EMPTY_ECONOMIC_POOL" if not order else "POOL_UNCERTAIN")}


def update_missing_counters(counters: dict, held: list[str], batch_healthy: bool, score: dict | None,
                            pool: dict) -> tuple[dict, dict]:
    """D05.4：返回 (新计数, 每股处置)。处置：RANK / EXIT_MODEL / HOLD_NO_ADD / EXIT_PERSISTENT / UNCHANGED。"""
    new = dict(counters)
    action = {}
    for sid in held:
        if not batch_healthy:
            action[sid] = "GLOBAL_UNHEALTHY"          # 不增不清
            continue
        if sid not in pool["size_order"]:
            action[sid] = "OFF_POOL"
            new.pop(sid, None)
            continue
        if sid in score["score_fraction"] or sid in score["ranks"]:
            new.pop(sid, None)
            action[sid] = "RANK"
            continue
        statuses = {st for _, st in score["invalid_reasons"].get(sid, [("?", SOURCE_UNKNOWN)])}
        if statuses & {MODEL_INAPPLICABLE, STALE}:
            action[sid] = "EXIT_MODEL"
            new.pop(sid, None)
            continue
        c = new.get(sid, 0) + 1
        new[sid] = c
        action[sid] = "HOLD_NO_ADD" if c < UNKNOWN_HEALTHY_WEEKS else "EXIT_PERSISTENT"
    for sid in list(new):
        if sid not in held:
            del new[sid]
    return new, action


def package_availability(week_statuses: list[str]) -> dict:
    """D05.5：week_statuses 为每个应评估周的 HEALTHY/BAD/UNASSESSED。先列已确认失败，再列未解决证据。"""
    total = len(week_statuses)
    healthy = week_statuses.count("HEALTHY")
    unassessed = week_statuses.count("UNASSESSED")
    streak = best = 0
    for s in week_statuses:
        streak = streak + 1 if s == "BAD" else 0
        best = max(best, streak)
    failures, unresolved = [], []
    if total == 0:
        unresolved.append("NO_ASSESSABLE_WEEKS")
    elif 20 * (healthy + unassessed) < 19 * total:
        failures.append("HEALTHY_RATIO_BELOW_95")
    elif 20 * healthy < 19 * total:
        unresolved.append("UNASSESSED_WEEKS_DECISIVE")
    if best > MAX_BAD_STREAK:
        failures.append("BAD_COVERAGE_STREAK_3")
    if unassessed and not failures:
        unresolved.append("UNASSESSED_WEEKS")
    status = "FAIL" if failures else ("INSUFFICIENT_EVIDENCE" if unresolved else "PASS")
    return {"status": status, "assessed_weeks": total, "healthy_weeks": healthy, "unassessed_weeks": unassessed,
            "healthy_ratio": Fraction(healthy, total) if total else None, "longest_bad_streak": best,
            "failures": failures, "unresolved": unresolved}
