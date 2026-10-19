"""来源标准化、QMT优先/Tushare补充的来源裁决、as-of 历史版本、财报衍生量（D03、D15）。

纯函数：不访问网络、不读系统时间。来源记录先标准化为 ObservedValue，再在统一观测层一次裁决。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from typing import Iterable

from .clock import TradingCalendar, add_months
from .contracts import (OK, UNKNOWN, ContractError, Reason, Result, money, parse_ts, reason, sh_dt)

QMT, TUSHARE, BROKER = "QMT", "TUSHARE", "BROKER_STATEMENT"

# 字段单位映射（D03.4）：仅为合同规定的转换，不代表本机接口已核验。
TUSHARE_UNIT_MULTIPLIERS = {
    ("daily", "vol"): ("volume_shares", Decimal(100)),         # 手 → 股
    ("daily", "amount"): ("amount_cny", Decimal(1000)),        # 千元 → 元
    ("daily_basic", "total_mv"): ("market_value_total", Decimal(10000)),  # 万元 → 元
    ("daily_basic", "turnover_rate_f"): ("turnover_free", Decimal("0.01")),  # % → 小数
}

# 只能来自券商确认记录的账户事实
ACCOUNT_FIELDS = frozenset({"broker_cash", "broker_available_cash", "broker_frozen_cash", "position_volume",
                            "position_can_use", "order_status", "trade_fill", "fee_actual"})


@dataclass(frozen=True)
class ObservedValue:
    record_id: str
    security_id: str
    field: str
    period: str                     # 交易日或报告期
    source: str                     # QMT / TUSHARE / BROKER_STATEMENT / TEST
    value: Decimal | None
    unit: str
    quantum: Decimal | None = None  # 来源展示精度
    basis_id: str = "RAW"
    available_at: datetime | None = None
    public_at: datetime | None = None
    source_revision: str = "0"
    quality: str = "OK"             # OK / PROVISIONAL / UNKNOWN / ERROR / STALE
    historical_semantics: bool = True
    evidence_ref: str = ""


def normalize(raw: dict, mapping: dict) -> ObservedValue:
    """来源行 → ObservedValue。mapping 来自 SourcesProfile.FieldMapping；单位按 multiply 转换。"""
    if mapping["source_unit"] == mapping["canonical_unit"] and money(mapping["multiply"]) != 1:
        raise ContractError("UNIT_MAPPING_INCONSISTENT", mapping["canonical_field"])
    v = raw.get(mapping["source_field"])
    value = None if v is None else money(v) * money(mapping["multiply"])
    avail = raw.get("available_at")
    if avail is not None:
        avail = parse_ts(avail)
    return ObservedValue(
        record_id=str(raw["record_id"]), security_id=str(raw["security_id"]), field=mapping["canonical_field"],
        period=str(raw["period"]), source=mapping["provider"], value=value, unit=mapping["canonical_unit"],
        quantum=money(raw["quantum"]) * money(mapping["multiply"]) if raw.get("quantum") is not None else None,
        basis_id=mapping["basis_id"], available_at=avail, source_revision=str(raw.get("source_revision", "0")),
        quality=raw.get("quality", "OK"), historical_semantics=mapping["historical_semantics"] != "CURRENT_ONLY",
        evidence_ref=mapping["evidence_ref"])


def tushare_mapping(endpoint: str, field_name: str, basis_id: str = "RAW") -> dict:
    canonical, mult = TUSHARE_UNIT_MULTIPLIERS[(endpoint, field_name)]
    return {"canonical_field": canonical, "provider": TUSHARE, "endpoint": endpoint, "source_field": field_name,
            "source_unit": endpoint + "." + field_name, "canonical_unit": canonical, "multiply": str(mult),
            "basis_id": basis_id, "historical_semantics": "HISTORICAL", "scope_ref": "contract",
            "evidence_ref": "D03.4"}


# ---------------------------------------------------------------- 来源裁决
@dataclass(frozen=True)
class SourceDecision:
    value: Decimal | None
    source: str | None
    action: str                      # PRIMARY / SUBSTITUTE / ROUNDING_EXPLAINED / UNKNOWN / CONFLICT
    reason_code: str = ""
    trail: tuple = ()


def _valid(o: ObservedValue | None, need_hist: bool, validity) -> tuple[bool, str]:
    if o is None or o.value is None:
        return False, "FIELD_MISSING"
    if o.quality in ("ERROR",):
        return False, "PROVEN_ERROR"
    if o.quality == "STALE":
        return False, "SOURCE_STALE"
    if o.quality == "UNKNOWN":
        return False, "QUALITY_UNKNOWN"
    if need_hist and not o.historical_semantics:
        return False, "HISTORICAL_SEMANTICS_UNAVAILABLE"
    if validity is not None and not validity(o.value):
        return False, "PROVEN_ERROR"
    return True, ""


def _interval(o: ObservedValue) -> tuple[Decimal, Decimal]:
    h = (o.quantum or Decimal(0)) / 2
    return o.value - h, o.value + h


def select_source(field_name: str, qmt: ObservedValue | None, ts: ObservedValue | None, *,
                  cutoff: datetime | None = None, need_historical: bool = True,
                  validity=None) -> Result:
    """D03.3 一次决定来源；QMT优先，同口径Tushare补充；实质冲突不掩盖。"""
    if field_name in ACCOUNT_FIELDS:
        src = qmt if (qmt is not None and qmt.source in (QMT, BROKER)) else None
        if src is None or src.value is None:
            return Result(UNKNOWN, SourceDecision(None, None, "UNKNOWN", "ACCOUNT_FACT_UNAVAILABLE"),
                          [reason("ACCOUNT_FACT_UNAVAILABLE", "FIELD", field_name,
                                  "账户事实只取券商确认记录，Tushare不能补造")])
        return Result(OK, SourceDecision(src.value, src.source, "PRIMARY", "", (src.record_id,)))

    def visible(o):
        if o is None:
            return None
        if cutoff is not None and (o.available_at is None or o.available_at > cutoff):
            return None
        return o

    qmt, ts = visible(qmt), visible(ts)
    if qmt is not None and ts is not None and (qmt.basis_id != ts.basis_id or qmt.unit != ts.unit):
        q_ok, _ = _valid(qmt, need_historical, validity)
        if q_ok:
            # 不同口径不可互相检查或替代；QMT自身合格即用QMT，但记录口径不一致
            return Result(OK, SourceDecision(qmt.value, QMT, "PRIMARY", "BASIS_MISMATCH_CROSSCHECK_SKIPPED",
                                             (qmt.record_id,)),
                          [reason("BASIS_MISMATCH", "FIELD", field_name, "交叉检查来源口径不同")])
        return Result(UNKNOWN, SourceDecision(None, None, "UNKNOWN", "BASIS_MISMATCH"),
                      [reason("BASIS_MISMATCH", "FIELD", field_name, "替代来源口径不同，不能替代")])
    q_ok, q_why = _valid(qmt, need_historical, validity)
    t_ok, t_why = _valid(ts, need_historical, validity)
    if q_ok and t_ok:
        if qmt.value == ts.value:
            return Result(OK, SourceDecision(qmt.value, QMT, "PRIMARY", "", (qmt.record_id, ts.record_id)))
        a, b = _interval(qmt), _interval(ts)
        if (qmt.quantum or ts.quantum) and a[0] <= b[1] and b[0] <= a[1]:
            return Result(OK, SourceDecision(qmt.value, QMT, "ROUNDING_EXPLAINED", "DISPLAY_ROUNDING",
                                             (qmt.record_id, ts.record_id)))
        return Result(UNKNOWN, SourceDecision(None, None, "CONFLICT", "SOURCE_CONFLICT",
                                              (qmt.record_id, ts.record_id)),
                      [reason("SOURCE_CONFLICT", "FIELD", field_name, f"QMT={qmt.value} TS={ts.value}")])
    if q_ok:
        return Result(OK, SourceDecision(qmt.value, QMT, "PRIMARY", "", (qmt.record_id,)))
    if t_ok:
        code = q_why if qmt is not None else "QMT_FIELD_MISSING"
        return Result(OK, SourceDecision(ts.value, TUSHARE, "SUBSTITUTE", code, (ts.record_id,)),
                      [reason("SUBSTITUTE", "FIELD", field_name, code)])
    return Result(UNKNOWN, SourceDecision(None, None, "UNKNOWN", q_why or t_why),
                  [reason("SOURCE_UNKNOWN", "FIELD", field_name, f"QMT:{q_why} TS:{t_why}")])


def build_facts(rows: Iterable[dict], mappings: list[dict]) -> tuple[list[ObservedValue], list[dict]]:
    """build-facts：只读来源行 → 标准化观测 → 每(证券,字段,期间)一次来源裁决，保留来源轨迹（D03、D21.1）。

    行格式：{record_id, provider, endpoint, source_field, security_id, period, value, available_at,
    [quantum], [quality], [source_revision]}；映射取自 SourcesProfile.mappings，未映射的行报 MAPPING_MISSING。
    同一来源同一期间多版本取最高 source_revision；同版本不同值为 SOURCE_CONFLICT。"""
    idx = {(m["provider"], m["endpoint"], m["source_field"]): m for m in mappings}
    obs, unmapped = [], []
    for r in rows:
        m = idx.get((r["provider"], r["endpoint"], r["source_field"]))
        if m is None:
            unmapped.append(r["record_id"])
            continue
        raw = {"record_id": r["record_id"], "security_id": r["security_id"], "period": r["period"],
               m["source_field"]: r.get("value"), "available_at": r.get("available_at"),
               "quantum": r.get("quantum"), "quality": r.get("quality", "OK"),
               "source_revision": r.get("source_revision", "0")}
        obs.append(normalize(raw, m))
    groups: dict = {}
    for o in obs:
        groups.setdefault((o.security_id, o.field, o.period), {}).setdefault(o.source, []).append(o)
    decisions = []
    for (sid, fld, per), by_src in sorted(groups.items()):
        picked, conflicts = {}, []
        for src, items in by_src.items():
            top = max(int(x.source_revision) if str(x.source_revision).isdigit() else 0 for x in items)
            cand = [x for x in items if (int(x.source_revision) if str(x.source_revision).isdigit() else 0) == top]
            if len({x.value for x in cand}) > 1:
                conflicts.append(src)
            picked[src] = cand[0]
        if conflicts:
            decisions.append({"security_id": sid, "field": fld, "period": per, "value": None, "source": None,
                              "action": "CONFLICT", "reason_code": "SOURCE_CONFLICT",
                              "trail": sorted(x.record_id for v in by_src.values() for x in v)})
            continue
        if "TEST" in picked:
            # TEST_ONLY 夹具来源只用于工程联调：不得与真实来源混用；单独出现时按同一有效性规则裁决并如实标为 TEST
            if len(picked) > 1:
                decisions.append({"security_id": sid, "field": fld, "period": per, "value": None, "source": None,
                                  "action": "UNKNOWN", "reason_code": "TEST_SOURCE_MIXED_WITH_REAL",
                                  "trail": sorted(x.record_id for v in by_src.values() for x in v)})
                continue
            res = select_source(fld, picked["TEST"], None)
            label = "TEST" if res.value.source is not None else None
        else:
            res = select_source(fld, picked.get(QMT) or picked.get(BROKER), picked.get(TUSHARE))
            label = res.value.source
        dv = res.value
        decisions.append({"security_id": sid, "field": fld, "period": per,
                          "value": None if dv.value is None else format(dv.value, "f"), "source": label,
                          "action": dv.action, "reason_code": dv.reason_code, "trail": list(dv.trail),
                          "reasons": [x.code for x in res.reasons]})
    for rid in unmapped:
        decisions.append({"record_id": rid, "action": "UNKNOWN", "reason_code": "MAPPING_MISSING"})
    return obs, decisions


def realtime_quote_usable(quote_time: datetime, now: datetime, max_age_seconds: int = 3,
                          kind: str = "TICK") -> bool:
    """实时报单的报价新鲜度；历史日值不能冒充实时对手盘。"""
    if kind != "TICK":
        return False
    age = (now - quote_time).total_seconds()
    return 0 <= age <= max_age_seconds


def merge_basis_series(a_basis: str, b_basis: str) -> Result:
    """两条复权序列锚不同不得直接拼接。"""
    if a_basis != b_basis:
        return Result(UNKNOWN, None, [reason("BASIS_MISMATCH", "SERIES", f"{a_basis}|{b_basis}")])
    return Result(OK, a_basis)


# ---------------------------------------------------------------- 分红来源去重
def merge_dividend_rows(rows: Iterable[dict]) -> Result:
    """同一事件的合计行与分项行按事件ID与条款去重；不得全部相加。
    rows: {event_id, component: 'TOTAL'|'PART', part_id?, cash_div_tax}"""
    by_event: dict[str, dict] = {}
    for r in rows:
        e = by_event.setdefault(r["event_id"], {"total": set(), "parts": {}})
        v = money(r["cash_div_tax"])
        if r["component"] == "TOTAL":
            e["total"].add(v)
        else:
            pid = r["part_id"]
            if pid in e["parts"] and e["parts"][pid] != v:
                return Result(UNKNOWN, None, [reason("SOURCE_CONFLICT", "EVENT", r["event_id"], "分项冲突")])
            e["parts"][pid] = v
    out, reasons = {}, []
    for eid, e in by_event.items():
        if len(e["total"]) > 1:
            reasons.append(reason("SOURCE_CONFLICT", "EVENT", eid, "合计冲突"))
            continue
        parts_sum = sum(e["parts"].values(), Decimal(0)) if e["parts"] else None
        if e["total"]:
            total = next(iter(e["total"]))
            if parts_sum is not None and parts_sum != total:
                reasons.append(reason("SOURCE_CONFLICT", "EVENT", eid, "分项合计不等于合计"))
                continue
            out[eid] = total
        else:
            out[eid] = parts_sum
    return Result(OK if not reasons else UNKNOWN, out, reasons)


# ---------------------------------------------------------------- 信息可用时间
def announcement_available_at(cal: TradingCalendar, announce_date: date | None,
                              public_at: datetime | None = None) -> datetime:
    """有可信公开时刻用该时刻；只有公告日期则为其后首个市场日09:30。"""
    if public_at is not None:
        return public_at
    if announce_date is None:
        raise ContractError("AVAILABILITY_UNKNOWN")
    return sh_dt(cal.next_session(announce_date), time(9, 30))


@dataclass(frozen=True)
class StatementRecord:
    security_id: str
    statement_type: str
    period: date                 # 报告期末
    scope: str                   # 合并口径
    field: str
    value: Decimal | None
    available_at: datetime
    version: int = 0             # 明确版本序号；None 表示无法确定修订关系
    supersedes: int | None = None
    source: str = TUSHARE
    record_id: str = ""


def statement_asof(records: Iterable[StatementRecord], key: tuple, cutoff: datetime) -> Result:
    """key=(security_id, statement_type, period, scope, field)；仅 available_at<=cutoff 的版本内取最新。"""
    cand = [r for r in records if (r.security_id, r.statement_type, r.period, r.scope, r.field) == key
            and r.available_at <= cutoff]
    if not cand:
        return Result(UNKNOWN, None, [reason("NOT_YET_VISIBLE", "STATEMENT", str(key))])
    latest_t = max(r.available_at for r in cand)
    top = [r for r in cand if r.available_at == latest_t]
    values = {r.value for r in top}
    if len(values) == 1:
        return Result(OK, top[0])
    # 同一公开时刻多个值：只有存在明确版本关系才能选择
    versions = [r for r in top if r.version is not None]
    if len(versions) == len(top):
        best = max(versions, key=lambda r: r.version)
        others = [r for r in top if r is not best]
        if all(best.supersedes is not None and (best.supersedes >= o.version) for o in others):
            return Result(OK, best)
    return Result(UNKNOWN, None, [reason("SOURCE_CONFLICT", "STATEMENT", str(key), "同键同时间不同值且无版本关系")])


def latest_visible_period(records: Iterable[StatementRecord], security_id: str, statement_type: str,
                          scope: str, field: str, cutoff: datetime) -> date | None:
    ps = [r.period for r in records if r.security_id == security_id and r.statement_type == statement_type
          and r.scope == scope and r.field == field and r.available_at <= cutoff]
    return max(ps) if ps else None


def single_quarter(cum: dict[int, tuple[Decimal | None, datetime | None]], quarter: int) -> Result:
    """累计转单季：Q1直接；Qn=累计n−累计n−1，所有输入须已公开。cum: {q: (value, available_at)}"""
    cur = cum.get(quarter)
    if cur is None or cur[0] is None:
        return Result(UNKNOWN, None, [reason("FACTOR_UNAVAILABLE", "STATEMENT", f"Q{quarter}")])
    if quarter == 1:
        return Result(OK, (cur[0], cur[1]))
    prev = cum.get(quarter - 1)
    if prev is None or prev[0] is None:
        return Result(UNKNOWN, None, [reason("FACTOR_UNAVAILABLE", "STATEMENT", f"Q{quarter - 1}",
                                             "缺上一季度累计，不能把累计当单季")])
    return Result(OK, (cur[0] - prev[0], max(cur[1], prev[1])))


def ttm(current_cum: tuple, prev_full_year: tuple, prev_same_cum: tuple, quarter: int) -> Result:
    """TTM=当期累计+上年全年−上年同期累计；Q4直接用全年。各元组为(value, available_at)。"""
    if quarter == 4:
        if current_cum[0] is None:
            return Result(UNKNOWN, None, [reason("FACTOR_UNAVAILABLE", "STATEMENT", "Q4")])
        return Result(OK, current_cum)
    parts = [current_cum, prev_full_year, prev_same_cum]
    if any(p is None or p[0] is None for p in parts):
        return Result(UNKNOWN, None, [reason("FACTOR_UNAVAILABLE", "STATEMENT", "TTM")])
    return Result(OK, (current_cum[0] + prev_full_year[0] - prev_same_cum[0], max(p[1] for p in parts)))


def ttm_crosscheck(ttm_value: Decimal, quarters: list[Decimal]) -> bool:
    """四个单季求和须与TTM同口径相等，否则拒绝该量。"""
    return sum(quarters, Decimal(0)) == ttm_value


def financial_fresh(period_end: date, cutoff_date: date, max_months: int = 15) -> bool:
    """报告期加 max_months 自然月的当天结束前有效；包只可更严（max_months<=15）。"""
    if max_months > 15:
        raise ContractError("FRESHNESS_TOO_LOOSE", str(max_months))
    return cutoff_date <= add_months(period_end, max_months)


def derived_available_at(*deps: datetime) -> datetime:
    return max(deps)


def ratio_validity(numerator: Decimal | None, denominator: Decimal | None, require_positive_denominator=True):
    """公共数学规则：分母为0或经济上要求为正但非正 → MODEL_INAPPLICABLE；合法负分子保留。"""
    if numerator is None or denominator is None:
        return "SOURCE_UNKNOWN"
    if denominator == 0 or (require_positive_denominator and denominator <= 0):
        return "MODEL_INAPPLICABLE"
    return "VALID"


# ---------------------------------------------------------------- 修复影响与重放范围
@dataclass(frozen=True)
class DecisionUsage:
    decision_id: str
    cutoff: datetime
    keys: frozenset              # {(field, security_id, period)}


def impact_scope(corrections: Iterable[tuple], usages: list[DecisionUsage]) -> Result:
    """D03.6：找到最早受影响的决策；从其之前的账户快照重放至D。无影响则不触发重放。"""
    changed = set(corrections)
    affected = sorted((u for u in usages if u.keys & changed), key=lambda u: u.cutoff)
    if not affected:
        return Result(OK, {"impact": "NO_IMPACT", "replay_from": None, "superseded": []},
                      [reason("NO_DECISION_IMPACT", "STUDY", "", "修正字段未被任何决策使用")])
    first = affected[0]
    superseded = [u.decision_id for u in usages if u.cutoff >= first.cutoff]
    return Result(OK, {"impact": "REPLAY_REQUIRED", "replay_from": first.decision_id,
                       "replay_from_cutoff": first.cutoff, "superseded": superseded})
