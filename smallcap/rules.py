"""RuleBook：按日期唯一匹配交易规则、数量网格、价格档与涨跌停、清算（D07.3、D15.3、H04.1—4.2）。

不存在“找不到就用10%涨跌停、100股、当前税率”的宽松分支。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from .contracts import ContractError, money, parse_date, parse_ts


def ceil_tick(p: Decimal, tick: Decimal) -> Decimal:
    return (p / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def floor_tick(p: Decimal, tick: Decimal) -> Decimal:
    return (p / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def half_up_tick(p: Decimal, tick: Decimal) -> Decimal:
    return (p / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


@dataclass(frozen=True)
class QuantityGrid:
    buy_min: int
    buy_step: int
    sell_min: int
    sell_step: int
    allow_full_odd_exit: bool
    max_order_quantity: int

    def legal_buy(self, b: int) -> bool:
        return b == 0 or (self.buy_min <= b <= self.max_order_quantity and (b - self.buy_min) % self.buy_step == 0)

    def max_buy(self, upper: int) -> int:
        """不超过 upper 的最大合法买入申报量（0 表示不足最小单位）。"""
        upper = min(upper, self.max_order_quantity)
        if upper < self.buy_min:
            return 0
        return self.buy_min + ((upper - self.buy_min) // self.buy_step) * self.buy_step

    def legal_sell(self, s: int, free_sell: int, total_qty: int) -> bool:
        if s == 0:
            return True
        if s > free_sell or s > self.max_order_quantity:
            return False
        if s == total_qty == free_sell and self.allow_full_odd_exit:
            return True
        return s >= self.sell_min and (s - self.sell_min) % self.sell_step == 0

    def max_sell(self, upper: int, free_sell: int, total_qty: int) -> int:
        """不超过 upper 的最大合法卖量。全部持仓可卖时允许一次性全部卖出零股。"""
        upper = min(upper, free_sell, self.max_order_quantity)
        if upper <= 0:
            return 0
        if upper == total_qty == free_sell and self.allow_full_odd_exit:
            return upper
        if upper < self.sell_min:
            return 0
        return self.sell_min + ((upper - self.sell_min) // self.sell_step) * self.sell_step

    def min_sell_at_least(self, need: int, free_sell: int, total_qty: int) -> int | None:
        """不少于 need 的最小合法卖量；不存在返回 None。"""
        if need <= 0:
            return 0
        cap = min(free_sell, self.max_order_quantity)
        if need <= self.sell_min:
            s = self.sell_min
        else:
            k = -(-(need - self.sell_min) // self.sell_step)
            s = self.sell_min + k * self.sell_step
        if s <= cap:
            return s
        if total_qty == free_sell and self.allow_full_odd_exit and need <= free_sell <= self.max_order_quantity:
            return free_sell
        return None

    def sell_sequence(self, base: int, free_sell: int, total_qty: int, cap: int) -> list[int]:
        """使合并申报量 base+x 合法、且不超过 cap 的追加卖量 x 序列（升序）。"""
        out = []
        top = min(free_sell, self.max_order_quantity, cap)
        s = self.sell_min
        while s <= top:
            if s > base:
                out.append(s - base)
            s += self.sell_step
        if (total_qty == free_sell and self.allow_full_odd_exit and free_sell <= min(self.max_order_quantity, cap)
                and free_sell > base and (free_sell - base) not in out):
            out.append(free_sell - base)
        return sorted(out)


@dataclass(frozen=True)
class TradeRule:
    rule_id: str
    market: str
    board: str
    scope: str
    effective_from: date
    effective_to: date
    public_at: datetime
    grid: QuantityGrid
    tick: Decimal
    limit_method: str
    up_percent: Decimal | None
    down_percent: Decimal | None
    bound_rounding: str
    settlement_id: str
    marketcap_floor: Decimal | None
    marketcap_floor_status: str

    def price_bounds(self, reference_preclose: Decimal | None, provided_upper: Decimal | None = None,
                     provided_lower: Decimal | None = None) -> tuple[Decimal | None, Decimal | None]:
        """法定价格上下限；方法未知或参考价缺失返回 (None, None) 由调用方阻断。"""
        if self.limit_method == "NONE":
            return Decimal(0), Decimal("Infinity")
        if self.limit_method == "EXCHANGE_BOUNDS":
            return provided_lower, provided_upper
        if self.limit_method == "PERCENT_REFERENCE":
            if reference_preclose is None or self.up_percent is None or self.down_percent is None:
                return None, None
            if self.bound_rounding != "HALF_UP_TICK":
                raise ContractError("RULE_UNSUPPORTED", self.bound_rounding)
            up = half_up_tick(reference_preclose * (1 + self.up_percent), self.tick)
            lo = half_up_tick(reference_preclose * (1 - self.down_percent), self.tick)
            return lo, up
        raise ContractError("RULE_UNSUPPORTED", self.limit_method)


# 已实现的清算日期映射（H04.2）：取值=相对成交日的市场日偏移。未识别取值返回 RULE_UNSUPPORTED，不默认按成交日。
TAX_DATE_FIELDS = {"TRADE_DATE": 0, "NEXT_SESSION": 1}
FEE_SETTLEMENT_TIMINGS = {"WITH_TRADE": 0, "NEXT_SESSION": 1}


@dataclass(frozen=True)
class Settlement:
    settlement_id: str
    buy_sellable_delay_sessions: int
    sell_proceeds_trade_delay_sessions: int
    sell_proceeds_withdraw_delay_sessions: int
    tax_acquisition_date_field: str
    tax_delivery_date_field: str
    fee_settlement_timing: str

    def __post_init__(self):
        for f in (self.tax_acquisition_date_field, self.tax_delivery_date_field):
            if f not in TAX_DATE_FIELDS:
                raise ContractError("RULE_UNSUPPORTED", f"{self.settlement_id}: tax date field {f}")
        if self.fee_settlement_timing not in FEE_SETTLEMENT_TIMINGS:
            raise ContractError("RULE_UNSUPPORTED", f"{self.settlement_id}: fee timing {self.fee_settlement_timing}")

    def dates(self, cal, d: date) -> dict:
        """一笔成交在日期 d 的可卖、回款可用、税务取得/交割与费用实扣日期（ISO 字符串；None=当日即可）。"""
        def nth(k):
            return cal.next_session(d, k).isoformat() if k else d.isoformat()
        return {"sellable_from": nth(self.buy_sellable_delay_sessions),
                "proceeds_tradeable_from": nth(self.sell_proceeds_trade_delay_sessions)
                if self.sell_proceeds_trade_delay_sessions else None,
                "tax_acquisition_date": nth(TAX_DATE_FIELDS[self.tax_acquisition_date_field]),
                "tax_delivery_date": nth(TAX_DATE_FIELDS[self.tax_delivery_date_field]),
                "fee_settlement": "WITH_TRADE" if self.fee_settlement_timing == "WITH_TRADE" else "ACCRUE",
                "fee_settle_on": nth(FEE_SETTLEMENT_TIMINGS[self.fee_settlement_timing])}


# 同期多套规则时的证券适用范围（2026-10-19 用户确认：按当时风险状态选择）
RULE_SCOPES = ("NORMAL", "RISK_WARNING", "DELISTING_ARRANGEMENT")


def scope_of_risk_status(st: str | None) -> str | None:
    if st is None:
        return None
    if st in ("ST", "STAR_ST"):
        return "RISK_WARNING"
    if st == "DELISTING_ARRANGEMENT":
        return "DELISTING_ARRANGEMENT"
    if st == "NORMAL":
        return "NORMAL"
    return None                      # TERMINATED 等：已无正常交易规则可选


class RuleBook:
    def __init__(self, payload: dict):
        if payload is None:
            raise ContractError("RULE_UNKNOWN", "RuleBook not provided")
        self.rulebook_id = payload["rulebook_id"]
        self.payload = payload
        self.settlements = {s["settlement_id"]: Settlement(
            s["settlement_id"], s["buy_sellable_delay_sessions"], s["sell_proceeds_trade_delay_sessions"],
            s["sell_proceeds_withdraw_delay_sessions"], s["tax_acquisition_date_field"],
            s["tax_delivery_date_field"], s["fee_settlement_timing"]) for s in payload["settlements"]}
        self.rules: list[TradeRule] = []
        for r in payload["rules"]:
            q, p = r["quantity_grid"], r["price_rule"]
            self.rules.append(TradeRule(
                r["rule_id"], r["market"], r["board"], r["security_scope_ref"], parse_date(r["effective_from"]),
                parse_date(r["effective_to"]), parse_ts(r["public_at"]),
                QuantityGrid(q["buy_min"], q["buy_step"], q["sell_min"], q["sell_step"], q["allow_full_odd_exit"],
                             q["max_order_quantity"]),
                money(p["tick"]), p["method"], money(p["up_percent"]) if p["up_percent"] is not None else None,
                money(p["down_percent"]) if p["down_percent"] is not None else None, p["bound_rounding"],
                r["settlement_id"], money(r["marketcap_floor"]) if r["marketcap_floor"] is not None else None,
                r["marketcap_floor_status"]))

    def lookup(self, market: str, board: str, d: date, used_at: datetime | None = None,
               risk_status=None) -> TradeRule:
        """每个实际使用日期必须唯一匹配；重叠 RULE_AMBIGUOUS，空档 RULE_UNKNOWN，未来公开拒用。
        同一市场/板块/日期有多套适用范围的规则时（如风险警示股另有涨跌幅），按该证券当时的风险状态选择：
        security_scope_ref 取 NORMAL / RISK_WARNING / DELISTING_ARRANGEMENT。risk_status 可为值或惰性函数；
        状态未知或没有对应范围的规则时不猜（RULE_UNKNOWN）。"""
        hits = [r for r in self.rules if r.market == market and r.board == board
                and r.effective_from <= d < r.effective_to]
        if not hits:
            raise ContractError("RULE_UNKNOWN", f"{market}/{board}/{d}")
        if len(hits) > 1:
            scopes = [r.scope for r in hits]
            if len(set(scopes)) != len(scopes):
                raise ContractError("RULE_AMBIGUOUS", f"{market}/{board}/{d}")    # 同一范围重叠：不能按文件先后取
            st = risk_status() if callable(risk_status) else risk_status
            scope = scope_of_risk_status(st)
            if scope is None:
                raise ContractError("RULE_UNKNOWN", f"{market}/{board}/{d}: 风险状态未知，无法选择适用范围")
            hits = [r for r in hits if r.scope == scope]
            if not hits:
                raise ContractError("RULE_UNKNOWN", f"{market}/{board}/{d}: 无 {scope} 范围规则")
            if len(hits) > 1:
                raise ContractError("RULE_AMBIGUOUS", f"{market}/{board}/{d}/{scope}")
        r = hits[0]
        if used_at is not None and r.public_at > used_at:
            raise ContractError("RULE_NOT_YET_PUBLIC", r.rule_id)
        return r

    def settlement(self, rule: TradeRule) -> Settlement:
        return self.settlements[rule.settlement_id]
