"""与因子无关的经济400池、三值资格与健康性（D04）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .clock import TradingCalendar
from .contracts import LISTING_AGE, MIN_ADV20, MIN_RAW_CLOSE, POOL_LIMIT, ContractError
from .rules import RuleBook

TRUE, FALSE, UNK = "TRUE", "FALSE", "UNKNOWN"
RISK_STATUS_BAD = frozenset({"ST", "STAR_ST", "DELISTING_ARRANGEMENT", "TERMINATED"})


@dataclass
class SecurityFacts:
    """某证券在信号时点可见的事实（已经过来源裁决与 as-of）。None 表示未知。"""
    security_id: str
    trade_code: str
    exchange: str                  # SH / SZ
    board: str                     # MAIN_SH / MAIN_SZ / CHINEXT / STAR
    security_type: str | None = "A_ORDINARY"
    listed: bool | None = True
    list_date: date | None = None
    industry_sw1: str | None = None
    risk_status: str | None = "NORMAL"
    raw_close: Decimal | None = None
    amounts_20: list = field(default_factory=list)   # [(date, amount|None, status TRADING/SUSPENDED/MISSING)]
    market_value_total: Decimal | None = None
    trading_status: str | None = "TRADING"


def _all(values: list[str]) -> str:
    if FALSE in values:
        return FALSE
    if UNK in values:
        return UNK
    return TRUE


def adv20(amounts_20: list) -> Decimal | None:
    """含信号日的最近20个市场日成交额之和÷20；确认停牌计0，来源缺失为未知。"""
    if len(amounts_20) < 20:
        return None
    tot = Decimal(0)
    for _, amt, status in amounts_20[-20:]:
        if status == "SUSPENDED":
            continue
        if amt is None or status == "MISSING":
            return None
        tot += amt
    return tot / 20


def eligibility(sf: SecurityFacts, d: date, cal: TradingCalendar, rulebook: RuleBook | None,
                allowed_boards: set, financial_codes: set, used_at=None) -> tuple[str, dict]:
    p: dict[str, str] = {}
    p["identity"] = TRUE if (sf.listed is True and sf.exchange in ("SH", "SZ") and sf.security_type == "A_ORDINARY") \
        else (FALSE if sf.listed is False or (sf.security_type not in (None, "A_ORDINARY")) else UNK)
    p["permission"] = TRUE if sf.board in allowed_boards else FALSE
    if sf.industry_sw1 is None:
        p["non_financial"] = UNK
    else:
        p["non_financial"] = FALSE if sf.industry_sw1 in financial_codes else TRUE
    if sf.list_date is None:
        p["listing_age"] = UNK
    else:
        first = cal.index_on_or_after(sf.list_date)
        age = cal.index(d) - first
        if age >= LISTING_AGE:
            p["listing_age"] = TRUE
        elif sf.list_date < cal.sessions[0]:
            p["listing_age"] = UNK           # 日历未覆盖上市日：预热不足是未知，不是不合格
        else:
            p["listing_age"] = FALSE
    p["risk_status"] = UNK if sf.risk_status is None else (FALSE if sf.risk_status in RISK_STATUS_BAD else TRUE)
    p["raw_close"] = UNK if sf.raw_close is None else (TRUE if sf.raw_close >= MIN_RAW_CLOSE else FALSE)
    a = adv20(sf.amounts_20)
    p["liquidity"] = UNK if a is None else (TRUE if a >= MIN_ADV20 else FALSE)
    if sf.market_value_total is None or sf.market_value_total <= 0:
        p["market_value"] = UNK
    elif rulebook is None:
        p["market_value"] = UNK
    else:
        try:
            r = rulebook.lookup(sf.exchange, sf.board, d, used_at, sf.risk_status)
            if r.marketcap_floor_status == "NOT_APPLICABLE":
                p["market_value"] = TRUE
            else:
                p["market_value"] = TRUE if sf.market_value_total >= r.marketcap_floor else FALSE
        except ContractError:
            p["market_value"] = UNK          # RULE_UNKNOWN/RULE_NOT_YET_PUBLIC ≠ NOT_APPLICABLE
    p["trading_today"] = UNK if sf.trading_status is None else (TRUE if sf.trading_status == "TRADING" else FALSE)
    return _all(list(p.values())), p


def build_pool(decision_id: str, cutoff_date: date, facts: list[SecurityFacts], cal: TradingCalendar,
               rulebook: RuleBook | None, allowed_boards: set, financial_codes: set,
               limit: int = POOL_LIMIT, used_at=None) -> dict:
    """确认合格者按(总市值, 代码)取最小的最多400只；未知若可能改变边界则 POOL_UNCERTAIN。
    used_at 为信号截止时刻：届时尚未公开的规则不得使用。"""
    ok, unknown, excluded, elig = [], [], {}, {}
    for sf in facts:
        overall, preds = eligibility(sf, cutoff_date, cal, rulebook, allowed_boards, financial_codes, used_at)
        elig[sf.security_id] = overall
        if overall == TRUE:
            ok.append(sf)
        elif overall == UNK:
            unknown.append(sf)
            excluded[sf.security_id] = [k for k, v in preds.items() if v == UNK]
        else:
            excluded[sf.security_id] = [k for k, v in preds.items() if v == FALSE]
    ok.sort(key=lambda s: (s.market_value_total, s.trade_code))
    members = ok[:limit]
    boundary = (members[-1].market_value_total, members[-1].trade_code) if len(members) == limit else None
    uncertain, irrelevant = [], []
    for sf in unknown:
        if boundary is not None and sf.market_value_total is not None and \
                (sf.market_value_total, sf.trade_code) > boundary:
            irrelevant.append(sf.security_id)
        else:
            uncertain.append(sf.security_id)
    if uncertain:
        health = "POOL_UNCERTAIN"
    elif not members:
        health = "EMPTY_ECONOMIC_POOL"
    else:
        health = "OK"
    return {"decision_id": decision_id, "cutoff": cutoff_date.isoformat(),
            "security_ids": [s.security_id for s in members],
            "size_order": {s.security_id: i + 1 for i, s in enumerate(members)},
            "size": len(members), "health": health, "uncertain": sorted(uncertain),
            "irrelevant_unknown": sorted(irrelevant), "exclusion_reasons": excluded, "eligibility": elig,
            "codes": {s.security_id: s.trade_code for s in facts},
            "industries": {s.security_id: s.industry_sw1 for s in facts},
            "market_values": {s.security_id: s.market_value_total for s in members}}
