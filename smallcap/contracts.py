"""V5D2 公共合同：固定常量、原因码、Result、精确数值与时间。

金额/价格使用十进制（Decimal，来自十进制字符串）；份额、NAV、HWM、DD、排名与
硬比例判定使用 Fraction（任意精度整数分子/正整数分母）。禁止 float、bool、NaN。
"""
from __future__ import annotations

import decimal
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any
from zoneinfo import ZoneInfo

DETAIL_VERSION = "V5D2"
SH_TZ = ZoneInfo("Asia/Shanghai")

# 足够大的精度，使有限十进制金额的加减乘保持精确。
decimal.getcontext().prec = 80

# ---------------------------------------------------------------- 固定常量
BASELINE: dict[str, Any] = {
    "pool_limit": 400,
    "slots": 30,
    "retain_rank": 45,
    "listing_age_sessions": 250,
    "min_raw_close": "2",
    "min_adv20": "10000000",
    "joint_coverage": "0.80",
    "package_healthy_ratio": "0.95",
    "max_consecutive_coverage_bad": 2,
    "unknown_healthy_weeks": 2,
    "financial_age_months": 15,
    "stock_target": "0.60",
    "stock_trigger": "0.65",
    "center": "0.02",
    "band_low": "0.015",
    "band_high": "0.025",
    "single_trigger": "0.05",
    "industry_limit": "0.20",
    "blocked_exit_trigger": "0.10",
    "dd_warning": "0.20",
    "dd_retire": "0.25",
    "dd_limit": "0.30",
    "startup_min_names": 24,
    "startup_min_stock_ratio": "0.50",
    "daily_capacity_ratio": "0.01",
    "commission_rate": "0.00025",
    "minimum_commission": "5",
    "base_slippage": "0.001",
    "high_slippage": "0.003",
    "cutoff_time": "20:00:00",
    "window_start": "10:00:00",
    "window_end": "10:30:00",
    "timezone": "Asia/Shanghai",
}

EXECUTION_TECHNICAL: dict[str, Any] = {
    "max_new_orders_per_security_side_window": 3,
    "order_wait_seconds": 60,
    "retry_min_seconds": 60,
    "quote_max_age_seconds": 3,
    "account_max_age_seconds": 5,
    "price_protection_nominal": "0.003",
    "protection_rounding": "OUTWARD_TICK",
}


def _fr(s: str) -> Fraction:
    return Fraction(Decimal(s))


POOL_LIMIT = BASELINE["pool_limit"]
SLOTS = BASELINE["slots"]
RETAIN_RANK = BASELINE["retain_rank"]
B2_RETAIN_RANK = SLOTS  # B2：不设45名保留区
LISTING_AGE = BASELINE["listing_age_sessions"]
MIN_RAW_CLOSE = Decimal(BASELINE["min_raw_close"])
MIN_ADV20 = Decimal(BASELINE["min_adv20"])
JOINT_COVERAGE = _fr(BASELINE["joint_coverage"])
PACKAGE_HEALTHY_RATIO = _fr(BASELINE["package_healthy_ratio"])
MAX_BAD_STREAK = BASELINE["max_consecutive_coverage_bad"]
UNKNOWN_HEALTHY_WEEKS = BASELINE["unknown_healthy_weeks"]
FINANCIAL_AGE_MONTHS = BASELINE["financial_age_months"]
STOCK_TARGET = _fr(BASELINE["stock_target"])
STOCK_TRIGGER = _fr(BASELINE["stock_trigger"])
CENTER = _fr(BASELINE["center"])
BAND_LOW = _fr(BASELINE["band_low"])
BAND_HIGH = _fr(BASELINE["band_high"])
SINGLE_TRIGGER = _fr(BASELINE["single_trigger"])
SINGLE_TARGET = CENTER
INDUSTRY_LIMIT = _fr(BASELINE["industry_limit"])
BLOCKED_EXIT_TRIGGER = _fr(BASELINE["blocked_exit_trigger"])
DD_WARNING = _fr(BASELINE["dd_warning"])
DD_RETIRE = _fr(BASELINE["dd_retire"])
DD_LIMIT = _fr(BASELINE["dd_limit"])
STARTUP_MIN_NAMES = BASELINE["startup_min_names"]
STARTUP_MIN_STOCK_RATIO = _fr(BASELINE["startup_min_stock_ratio"])
DAILY_CAPACITY_RATIO = Decimal(BASELINE["daily_capacity_ratio"])
COMMISSION_RATE = Decimal(BASELINE["commission_rate"])
MINIMUM_COMMISSION = Decimal(BASELINE["minimum_commission"])
BASE_SLIPPAGE = Decimal(BASELINE["base_slippage"])
HIGH_SLIPPAGE = Decimal(BASELINE["high_slippage"])
CUTOFF_TIME = time(20, 0, 0)
WINDOW_START = time(10, 0, 0)
WINDOW_END = time(10, 30, 0)
PROXY_TIME = time(9, 30, 0)

MAX_ORDERS_PER_SIDE_WINDOW = EXECUTION_TECHNICAL["max_new_orders_per_security_side_window"]
ORDER_WAIT_SECONDS = EXECUTION_TECHNICAL["order_wait_seconds"]
RETRY_MIN_SECONDS = EXECUTION_TECHNICAL["retry_min_seconds"]
QUOTE_MAX_AGE_SECONDS = EXECUTION_TECHNICAL["quote_max_age_seconds"]
ACCOUNT_MAX_AGE_SECONDS = EXECUTION_TECHNICAL["account_max_age_seconds"]
PRICE_PROTECTION = Decimal(EXECUTION_TECHNICAL["price_protection_nominal"])

START_YEARS = (2011, 2016, 2021, 2024)
START_IDS = {2011: "A0", 2016: "A1", 2021: "A2", 2024: "A3"}
ROLES = ("B0", "B1", "B2")
SCENARIOS = ("S0", "S1", "S2", "S3")
DEFAULT_CAPITALS = ("100000", "500000")
EXECUTION_MODES = ("REPLAY_DAILY_PROXY", "REPLAY_MINUTE_WINDOW", "FORWARD_MANUAL")

# ---------------------------------------------------------------- 状态枚举
OK, PARTIAL, UNKNOWN, INVALID, NOT_APPLICABLE = "OK", "PARTIAL", "UNKNOWN", "INVALID", "NOT_APPLICABLE"
RUN_STATES = ("WAITING_START", "NORMAL", "RESTRICTED", "EXITING", "RETIRED")
QUALIFICATION = ("ENGINEERING_ONLY", "INSUFFICIENT_EVIDENCE", "REJECTED", "TRADEOFF_PENDING", "REVIEWABLE")
PROFILE_STATUSES = ("NOT_PROVIDED", "DECLARED", "SAMPLE_CHECKED", "VERIFIED_FOR_SCOPE")

REASON_CODES = frozenset(
    """SOURCE_CONFLICT SOURCE_STALE BASIS_MISMATCH RULE_UNKNOWN POOL_UNCERTAIN JOINT_COVERAGE_LOW
    FLAT_SCORE FACTOR_UNAVAILABLE MODEL_INAPPLICABLE INSUFFICIENT_CASH SLOT_BLOCKED RISK_ACTIVE
    TAX_UNKNOWN CAPACITY_EXHAUSTED ORDER_STATE_UNKNOWN CAPITAL_CHANGED VALUATION_UNKNOWN
    CASH_RECONCILIATION_FAILED STARTUP_UNFIT RETIRED_TRIGGERED SCENARIO_INCOMPLETE""".split()
)


class ContractError(ValueError):
    """不可恢复的合同/格式错误；code 为机器可读原因码。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass(frozen=True)
class Reason:
    code: str
    scope_type: str = "ACCOUNT"
    scope_id: str = ""
    message: str = ""
    input_refs: tuple = ()

    def to_json(self) -> dict:
        return {"code": self.code, "scope_type": self.scope_type, "scope_id": self.scope_id,
                "message": self.message, "input_refs": list(self.input_refs)}


@dataclass
class Result:
    status: str
    value: Any = None
    reasons: list[Reason] = field(default_factory=list)
    effective_asof: Any = None
    input_revision: Any = None

    @property
    def ok(self) -> bool:
        return self.status == OK

    def codes(self) -> list[str]:
        return [r.code for r in self.reasons]


def reason(code: str, scope_type: str = "ACCOUNT", scope_id: str = "", message: str = "", refs=()) -> Reason:
    return Reason(code, scope_type, scope_id, message, tuple(refs))


# ---------------------------------------------------------------- 数值
_DEC_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_INT_RE = re.compile(r"^(?:0|-?[1-9][0-9]*)$")
_POS_RE = re.compile(r"^[1-9][0-9]*$")
CENT = Decimal("0.01")


def money(x: Any) -> Decimal:
    """严格读取十进制金额/价格。拒绝 float、bool、NaN、Infinity 及非规范字符串。"""
    if isinstance(x, bool) or isinstance(x, float):
        raise ContractError("TYPE_INVALID", f"inexact numeric {x!r}")
    if isinstance(x, Decimal):
        if not x.is_finite():
            raise ContractError("TYPE_INVALID", "non-finite decimal")
        return x
    if isinstance(x, int):
        return Decimal(x)
    if isinstance(x, str):
        if not _DEC_RE.fullmatch(x):
            raise ContractError("TYPE_INVALID", f"not a decimal string {x!r}")
        return Decimal(x)
    raise ContractError("TYPE_INVALID", f"unsupported money type {type(x).__name__}")


def shares(x: Any) -> int:
    """股数：非负整数，拒绝 bool/float/字符串小数。"""
    if isinstance(x, bool) or not isinstance(x, int):
        raise ContractError("TYPE_INVALID", f"share quantity must be int, got {x!r}")
    if x < 0:
        raise ContractError("TYPE_INVALID", "negative share quantity")
    return x


def ratio(x: Any) -> Fraction:
    """精确有理数：由十进制系数直接得到，不经过 Decimal 除法。"""
    if isinstance(x, bool) or isinstance(x, float):
        raise ContractError("TYPE_INVALID", f"inexact numeric {x!r}")
    if isinstance(x, Fraction):
        return x
    if isinstance(x, int):
        return Fraction(x)
    return Fraction(money(x))


def wire_ratio(x: Any) -> dict:
    f = ratio(x)
    return {"num": str(f.numerator), "den": str(f.denominator)}


def unwire_ratio(obj: Any) -> Fraction:
    """反序列化规范 ExactRatio；非规范（未约分、前导零、负零、负分母、数字字面量）一律拒绝。"""
    if not isinstance(obj, dict) or set(obj) != {"num", "den"}:
        raise ContractError("RATIO_SHAPE", repr(obj))
    num, den = obj["num"], obj["den"]
    if not isinstance(num, str) or not isinstance(den, str):
        raise ContractError("RATIO_FORMAT", "num/den must be decimal integer strings")
    if not _INT_RE.fullmatch(num) or not _POS_RE.fullmatch(den):
        raise ContractError("RATIO_FORMAT", repr(obj))
    f = Fraction(int(num), int(den))
    if wire_ratio(f) != obj:
        raise ContractError("RATIO_NOT_CANONICAL", repr(obj))
    return f


def cents(x: Decimal, mode: str = ROUND_HALF_UP) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=mode)


def quantize(x: Decimal, quantum: Decimal, mode: str = "HALF_UP") -> Decimal:
    rounding = {"HALF_UP": ROUND_HALF_UP, "FLOOR": ROUND_FLOOR, "CEILING": ROUND_CEILING}[mode]
    return (Decimal(x) / quantum).to_integral_value(rounding=rounding) * quantum


def frac_to_decimal(f: Fraction, places: int = 8) -> Decimal:
    """仅供展示（*_display）；不得回读进入决策。"""
    q = Decimal(1).scaleb(-places)
    return (Decimal(f.numerator) / Decimal(f.denominator)).quantize(q, rounding=ROUND_HALF_UP)


def dec_str(d: Decimal) -> str:
    """规范十进制字符串（不使用科学记数）。"""
    s = format(Decimal(d), "f")
    if s in ("-0", "-0.0") or (s.startswith("-") and Decimal(s) == 0):
        s = s[1:]
    return s


def frac_exact_decimal(f: Fraction) -> Decimal | None:
    """若 f 为有限十进制则返回精确 Decimal，否则 None。"""
    den = f.denominator
    for p in (2, 5):
        while den % p == 0:
            den //= p
    if den != 1:
        return None
    return Decimal(f.numerator) / Decimal(f.denominator)


# ---------------------------------------------------------------- 时间
def parse_ts(s: Any) -> datetime:
    """ISO8601 必须带时区；返回 UTC aware datetime。"""
    if isinstance(s, datetime):
        if s.tzinfo is None:
            raise ContractError("TIMEZONE_MISSING", s.isoformat())
        return s.astimezone(timezone.utc)
    if not isinstance(s, str):
        raise ContractError("TYPE_INVALID", f"timestamp {s!r}")
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ContractError("TYPE_INVALID", str(e)) from None
    if d.tzinfo is None:
        raise ContractError("TIMEZONE_MISSING", s)
    return d.astimezone(timezone.utc)


def parse_date(s: Any) -> date:
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if not isinstance(s, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
        raise ContractError("TYPE_INVALID", f"date {s!r}")
    return date.fromisoformat(s)


def sh_dt(d: date, t: time) -> datetime:
    """北京时间某日某时 → UTC aware。"""
    return datetime.combine(d, t, tzinfo=SH_TZ).astimezone(timezone.utc)


def sh_local(dt: datetime) -> datetime:
    return dt.astimezone(SH_TZ)


def ts_str(dt: datetime) -> str:
    return sh_local(dt).isoformat()
