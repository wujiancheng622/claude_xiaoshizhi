"""委托级累计计费与股息税规则（D11、H04.3—4.4）。

佣金按同一委托累计成交额计算：commission(A)=0(A=0) 否则 round_cent(max(rate×A, 最低))；
第k次新增费用=commission(A_k)−commission(A_{k−1})。股息税制按股权登记日唯一选择。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from fractions import Fraction

from .clock import add_months
from .contracts import (CENT, COMMISSION_RATE, MINIMUM_COMMISSION, ContractError, cents, money, parse_date,
                        quantize)


@dataclass(frozen=True)
class FeeRule:
    fee_id: str
    fee_kind: str
    effective_from: date
    effective_to: date
    market: str
    side: str
    base_kind: str
    rate: Decimal
    minimum: Decimal
    maximum: Decimal | None
    included_in_commission: bool
    charge_unit: str
    round_quantum: Decimal
    round_mode: str
    settlement_id: str

    def applies(self, market: str, side: str, d: date) -> bool:
        return (self.market in (market, "BOTH") and self.side in (side, "BOTH")
                and self.effective_from <= d < self.effective_to)

    def amount(self, notional: Decimal, qty: int, par_value: Decimal | None = None) -> Decimal:
        if notional == 0 and qty == 0:
            return Decimal(0)
        base = {"NOTIONAL": notional, "SHARES": Decimal(qty)}.get(self.base_kind)
        if self.base_kind == "PAR_VALUE":
            if par_value is None:
                raise ContractError("FEE_BASE_UNKNOWN", self.fee_id)
            base = par_value * qty
        raw = max(self.rate * base, self.minimum)
        if self.maximum is not None:
            raw = min(raw, self.maximum)
        return quantize(raw, self.round_quantum, self.round_mode)


def fee_rule_from_json(r: dict) -> FeeRule:
    return FeeRule(r["fee_id"], r["fee_kind"], parse_date(r["effective_from"]), parse_date(r["effective_to"]),
                   r["market"], r["side"], r["base_kind"], money(r["rate"]), money(r["minimum"]),
                   money(r["maximum"]) if r["maximum"] is not None else None, r["included_in_commission"],
                   r["charge_unit"], money(r["round_quantum"]), r["round_mode"], r["settlement_id"])


REQUIRED_KINDS = ("COMMISSION", "STAMP", "TRANSFER")


class FeeSchedule:
    """费用表；缺失区间 FEE_SCHEDULE_INCOMPLETE，不套今天费率。"""

    def __init__(self, payload: dict):
        if payload is None:
            raise ContractError("FEE_SCHEDULE_INCOMPLETE", "fee profile not provided")
        self.fee_profile_id = payload["fee_profile_id"]
        self.payload = payload
        self.purpose = payload["purpose"]
        self.rules = [fee_rule_from_json(r) for r in payload["fee_rules"]]
        for r in self.rules:
            if r.charge_unit not in ("ORDER_CUMULATIVE", "FILL"):
                raise ContractError("FEE_CHARGE_UNIT_UNSUPPORTED", f"{r.fee_id}:{r.charge_unit}")
            if r.fee_kind == "COMMISSION":
                if r.charge_unit != "ORDER_CUMULATIVE":
                    raise ContractError("FEE_RULE_INVALID", r.fee_id)
                if self.purpose != "BROKER_CONFIRMED" and (r.rate != COMMISSION_RATE or r.minimum != MINIMUM_COMMISSION):
                    raise ContractError("BASELINE_MISMATCH", f"research commission {r.fee_id}")
        self.tax = TaxSchedule(payload["dividend_tax_rules"])

    def _rules(self, market: str, side: str, d: date) -> list[FeeRule]:
        kinds: dict[str, list[FeeRule]] = {}
        for r in self.rules:
            if r.applies(market, side, d):
                kinds.setdefault(r.fee_kind, []).append(r)
        for k in REQUIRED_KINDS:
            if k not in kinds:
                # 不存在“找不到就用今天费率/记0”的分支
                raise ContractError("FEE_SCHEDULE_INCOMPLETE", f"{k}/{market}/{side}/{d}")
        for k, v in kinds.items():
            if len(v) > 1:
                raise ContractError("RULE_AMBIGUOUS", f"fee {k}/{market}/{side}/{d}")
        return [v[0] for v in kinds.values() if not v[0].included_in_commission]

    def order_fees(self, market: str, side: str, d: date, cum_notional: Decimal, cum_qty: int,
                   fills: list[tuple[Decimal, int]] | None = None) -> dict[str, Decimal]:
        """该委托在累计 (cum_notional, cum_qty) 时按费种应计的总费用。FILL 粒度按逐笔成交分别取整。"""
        out = {}
        for r in self._rules(market, side, d):
            if r.charge_unit == "ORDER_CUMULATIVE":
                out[r.fee_kind] = r.amount(cum_notional, cum_qty)
            else:
                out[r.fee_kind] = sum((r.amount(a, q) for a, q in (fills or [])), Decimal(0))
        return out

    def preview_new_order_fees(self, market: str, side: str, d: date, notional: Decimal, qty: int) -> Decimal:
        """新委托（A=0）若全部成交的费用（预约/预览用）。"""
        return sum(self.order_fees(market, side, d, notional, qty, [(notional, qty)]).values(), Decimal(0))

    def extra_fee_reserve(self, market: str, side: str, d: date, done_notional: Decimal, done_qty: int,
                          rest_notional: Decimal, rest_qty: int, accrued: Decimal,
                          done_fills: list[tuple[Decimal, int]] | None = None) -> Decimal:
        """部分成交委托剩余可能新增费用 max(g(A+B)−g(A),0)，已计提的不重复预约。"""
        fills = list(done_fills or [])
        total = sum(self.order_fees(market, side, d, done_notional + rest_notional, done_qty + rest_qty,
                                    fills + ([(rest_notional, rest_qty)] if rest_qty else [])).values(), Decimal(0))
        return max(total - accrued, Decimal(0))


def research_commission(cum_notional: Decimal) -> Decimal:
    """研究基线佣金（仅用于夹具与说明）。"""
    if cum_notional == 0:
        return Decimal(0)
    return cents(max(COMMISSION_RATE * cum_notional, MINIMUM_COMMISSION))


# ---------------------------------------------------------------- 股息税
@dataclass(frozen=True)
class TaxTier:
    max_months: int | None
    rate: Decimal


@dataclass(frozen=True)
class DividendTaxRule:
    rule_id: str
    effective_from: date
    effective_to: date
    security_class: str
    dividend_type: str
    initial_withholding_rate: Decimal
    tiers: tuple
    rounding_unit: str
    acquisition_inheritance_rule: str

    def final_rate(self, acquired: date, delivered: date) -> Decimal:
        if delivered < acquired:
            raise ContractError("TAX_DATE_REVERSED", f"{acquired}>{delivered}")
        for t in self.tiers:
            if t.max_months is None or delivered <= add_months(acquired, t.max_months):
                return t.rate
        raise ContractError("TAX_TIER_MISSING", self.rule_id)


class TaxSchedule:
    def __init__(self, rows: list[dict]):
        self.rules = []
        for r in rows:
            if r.get("selection_date_basis") != "EQUITY_RECORD_DATE":
                raise ContractError("TAX_SELECTION_BASIS", r.get("rule_id", "?"))
            f, t = parse_date(r["effective_from"]), parse_date(r["effective_to"])
            if f >= t:
                raise ContractError("TAX_RULE_INTERVAL", r["rule_id"])
            tiers = tuple(TaxTier(x["max_calendar_months_inclusive"], money(x["rate"])) for x in r["tiers"])
            if tiers[-1].max_months is not None:
                raise ContractError("TAX_TIER_MISSING", r["rule_id"])
            self.rules.append(DividendTaxRule(r["rule_id"], f, t, r["security_class"], r["dividend_type"],
                                              money(r["initial_withholding_rate"]), tiers, r["rounding_unit"],
                                              r["acquisition_inheritance_rule"]))

    def select(self, record_date: date, security_class: str = "INDIVIDUAL_A_SHARE",
               dividend_type: str = "CASH") -> DividendTaxRule:
        """按股权登记日选择：from<=record_date<to；0行 TAX_RULE_MISSING，多行 TAX_RULE_OVERLAP。"""
        hits = [r for r in self.rules if r.security_class == security_class and r.dividend_type == dividend_type
                and r.effective_from <= record_date < r.effective_to]
        if not hits:
            raise ContractError("TAX_RULE_MISSING", f"{record_date}/{security_class}/{dividend_type}")
        if len(hits) > 1:
            raise ContractError("TAX_RULE_OVERLAP", f"{record_date}: {[r.rule_id for r in hits]}")
        return hits[0]

    def by_id(self, rule_id: str) -> DividendTaxRule:
        for r in self.rules:
            if r.rule_id == rule_id:
                return r
        raise ContractError("TAX_RULE_MISSING", rule_id)


def allocate_int(total: int, weights: list[tuple[str, Fraction]]) -> dict[str, int]:
    """整数总量按比例分配：先向下取整，剩余单位按小数余数降序、再按 ID 升序分配；合计严格等于总量。"""
    wsum = sum((w for _, w in weights), Fraction(0))
    if wsum == 0:
        return {sid: 0 for sid, _ in weights}
    base, rema = {}, []
    for sid, w in weights:
        exact = Fraction(total) * w / wsum
        fl = exact.numerator // exact.denominator
        base[sid] = fl
        rema.append((-(exact - fl), sid))
    left = total - sum(base.values())
    for _, sid in sorted(rema)[:left]:
        base[sid] += 1
    return base


def allocate_cents(total: Decimal, weights: list[tuple[str, Fraction]]) -> dict[str, Decimal]:
    """按未取整理论额比例分摊已取整总额：先向下到分，剩余分按小数余数降序、再按 slice_id 分配。"""
    total_cents = int((total / CENT).to_integral_value())
    if Decimal(total_cents) * CENT != total:
        raise ContractError("ALLOCATION_NOT_CENTS", str(total))
    return {sid: Decimal(c) * CENT for sid, c in allocate_int(total_cents, weights).items()}


def tax_amount(base: Decimal, rate: Decimal) -> Decimal:
    return cents(base * rate)


def floor_cents(x: Decimal) -> Decimal:
    return cents(x, ROUND_FLOOR)
