"""执行层纯函数：有界限价、报价新鲜度、窗口、容量、费用后买入预览与顺序数量、首次资金适配（D07、D09、D10）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction

from .contracts import (ACCOUNT_MAX_AGE_SECONDS, CENTER, DAILY_CAPACITY_RATIO, INDUSTRY_LIMIT, PRICE_PROTECTION,
                        QUOTE_MAX_AGE_SECONDS, SINGLE_TRIGGER, STARTUP_MIN_NAMES, STARTUP_MIN_STOCK_RATIO,
                        STOCK_TARGET, WINDOW_END, WINDOW_START, ContractError, sh_local)
from .fees import FeeSchedule
from .rules import QuantityGrid, ceil_tick, floor_tick


# ---------------------------------------------------------------- 价格保护
def protection_bounds(anchor: Decimal, tick: Decimal, lower_legal: Decimal | None, upper_legal: Decimal | None):
    """锚±30bp 向外对齐一个价格档（买上界向上、卖下界向下），再与法定限制相交；不滚动扩大。"""
    buy_upper = ceil_tick(anchor * (1 + PRICE_PROTECTION), tick)
    sell_lower = floor_tick(anchor * (1 - PRICE_PROTECTION), tick)
    if upper_legal is not None:
        buy_upper = min(buy_upper, upper_legal)
    if lower_legal is not None:
        sell_lower = max(sell_lower, lower_legal)
    return buy_upper, sell_lower


def realtime_limit_price(side: str, bid: Decimal | None, ask: Decimal | None, tick: Decimal, buy_upper: Decimal,
                         sell_lower: Decimal) -> Decimal | None:
    """买用卖一向上对齐、不高于保护上界；卖用买一向下对齐、不低于保护下界；否则不报单。"""
    if side == "BUY":
        if ask is None or ask <= 0:
            return None
        p = ceil_tick(ask, tick)
        return p if p <= buy_upper else None
    if bid is None or bid <= 0:
        return None
    p = floor_tick(bid, tick)
    return p if p >= sell_lower else None


def quote_fresh(quote_time: datetime, now: datetime, mode: str = "TICK") -> bool:
    """实时 tick 报价不超过3秒；分钟代理显式另一模式，不套用3秒规则。"""
    if mode == "MINUTE_PROXY":
        return quote_time <= now
    if mode != "TICK":
        return False
    age = (now - quote_time).total_seconds()
    return 0 <= age <= QUOTE_MAX_AGE_SECONDS


def account_fresh(snapshot_time: datetime, now: datetime, unmerged_reports: int = 0) -> bool:
    return 0 <= (now - snapshot_time).total_seconds() <= ACCOUNT_MAX_AGE_SECONDS and unmerged_reports == 0


def can_submit_new(t: datetime) -> bool:
    """[10:00:00, 10:30:00)：10:30 及之后只能撤单与核对。"""
    lt = sh_local(t).time()
    return WINDOW_START <= lt < WINDOW_END


def proxy_price(side: str, open_px: Decimal, slippage: Decimal, tick: Decimal, lower_legal: Decimal | None,
                upper_legal: Decimal | None) -> Decimal | None:
    """日线/分钟代理成交价：开盘×(1±滑点)按不利方向取整；须在保护与法定边界内，否则不成交。"""
    buy_upper, sell_lower = protection_bounds(open_px, tick, lower_legal, upper_legal)
    if side == "BUY":
        if upper_legal is not None and open_px >= upper_legal:
            return None                     # 买在涨停不成交
        p = ceil_tick(open_px * (1 + slippage), tick)
        return p if p <= buy_upper else None
    if lower_legal is not None and open_px <= lower_legal:
        return None                         # 卖在跌停不成交
    p = floor_tick(open_px * (1 - slippage), tick)
    return p if p >= sell_lower else None


# ---------------------------------------------------------------- 容量
def capacity_cap(prior_20_amounts: list) -> Decimal | None:
    """cap=0.01×执行日前20个市场日均额；确认停牌计0；任一必要来源缺失则未知。"""
    if len(prior_20_amounts) < 20:
        return None
    tot = Decimal(0)
    for amt, status in prior_20_amounts[-20:]:
        if status == "SUSPENDED":
            continue
        if amt is None:
            return None
        tot += amt
    return DAILY_CAPACITY_RATIO * tot / 20


def capacity_left(cap: Decimal | None, used: Decimal, pending: Decimal) -> Decimal | None:
    if cap is None:
        return None
    return cap - used - pending


# ---------------------------------------------------------------- 费用后买入预览
@dataclass
class BookView:
    """一个一致估值时点的账户视图（从 Ledger 与当前价导出），用于买卖预览。"""
    equity: Fraction
    stock_value: Fraction
    holdings_value: dict          # sid -> Fraction（含冻结、残余、股份应收）
    industry_value: dict          # industry -> Fraction
    industry_of: dict             # sid -> industry
    open_buy_adj: Fraction        # Σ未结买单 rem×(m−p)−尚未入账费用
    open_buy_value: dict          # sid -> rem×m
    cash_free: Decimal            # min(策略可授权额, 券商可授权额)


def buy_ok(view: BookView, sid: str, industry: str, b: int, p: Decimal, m: Decimal, fee: Decimal) -> list[str]:
    """返回违反的约束列表（空表示通过）。E_after=E+b(m−p)−f；V_i_after=V_i+bm。"""
    bad = []
    e_after = view.equity + view.open_buy_adj + b * (Fraction(m) - Fraction(p)) - Fraction(fee)
    if e_after <= 0:
        return ["EQUITY_NONPOSITIVE"]
    stock_after = view.stock_value + sum(view.open_buy_value.values(), Fraction(0)) + b * Fraction(m)
    if stock_after > STOCK_TARGET * e_after:
        bad.append("STOCK_60")
    single_after = view.holdings_value.get(sid, Fraction(0)) + view.open_buy_value.get(sid, Fraction(0)) + b * Fraction(m)
    if single_after > SINGLE_TRIGGER * e_after:
        bad.append("SINGLE_5")
    ind_open = sum((v for s, v in view.open_buy_value.items() if view.industry_of.get(s) == industry), Fraction(0))
    if view.industry_value.get(industry, Fraction(0)) + ind_open + b * Fraction(m) > INDUSTRY_LIMIT * e_after:
        bad.append("INDUSTRY_20")
    if Decimal(b) * p + fee > view.cash_free:
        bad.append("INSUFFICIENT_CASH")
    return bad


def size_buy(view: BookView, *, sid: str, industry: str, grid: QuantityGrid, p: Decimal, m: Decimal,
             gap_value: Fraction, fee_fn, capacity_left_value: Decimal | None) -> tuple[int, list[str]]:
    """最大合法、满足全部约束且不超过目标缺口的买入数量。fee_fn(b)->新增费用。"""
    if gap_value <= 0:
        return 0, ["TARGET_REACHED"]
    upper = int(gap_value / Fraction(m))
    if capacity_left_value is None:
        return 0, ["CAPACITY_UNKNOWN"]
    if capacity_left_value <= 0:
        return 0, ["CAPACITY_EXHAUSTED"]
    if capacity_left_value.is_finite():
        upper = min(upper, int(capacity_left_value / p))
    b = grid.max_buy(upper)
    if b == 0:
        return 0, ["ROUNDING_NO_ORDER" if int(gap_value / Fraction(m)) < grid.buy_min else "CAPACITY_EXHAUSTED"]
    # 约束随 b 单调：二分合法网格索引
    lo, hi = 0, (b - grid.buy_min) // grid.buy_step       # 候选 b_k = buy_min + k*step
    last_bad = buy_ok(view, sid, industry, b, p, m, fee_fn(b))
    if not last_bad:
        return b, []
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        bk = grid.buy_min + mid * grid.buy_step
        why = buy_ok(view, sid, industry, bk, p, m, fee_fn(bk))
        if not why:
            best = bk
            lo = mid + 1
        else:
            last_bad = why
            hi = mid - 1
    if best is None:
        return 0, last_bad
    return best, []


# ---------------------------------------------------------------- 首次资金适配（D07.6）
def startup_fit(*, capital: Decimal, names: list[str], close: dict, industry_of: dict, grid_of, fee_fn,
                slippage: Decimal, tick_of) -> dict:
    """截止时固定名单与当日原始收盘价上作一次无市场受阻建仓测算。
    股票价值按 Σ股数×原始参考价/入金本金，不把滑点成本计入股票价值。"""
    cash = capital
    fees = Decimal(0)
    hold: dict = {}
    ind: dict = {}
    qty = {}
    target = CENTER * Fraction(capital)
    for sid in names:
        m = close.get(sid)
        if m is None:
            return {"status": "NOT_STARTED_UNKNOWN", "reason": f"REFERENCE_PRICE_UNKNOWN:{sid}"}
        p = ceil_tick(m * (1 + slippage), tick_of(sid))
        stock = sum(hold.values(), Fraction(0))
        equity = Fraction(cash) + stock
        view = BookView(equity=equity, stock_value=stock, holdings_value=dict(hold), industry_value=dict(ind),
                        industry_of=dict(industry_of), open_buy_adj=Fraction(0), open_buy_value={}, cash_free=cash)
        b, _ = size_buy(view, sid=sid, industry=industry_of[sid], grid=grid_of(sid), p=p, m=m, gap_value=target,
                        fee_fn=lambda x, sid=sid, p=p: fee_fn(sid, x, p), capacity_left_value=Decimal("Infinity"))
        if b:
            f = fee_fn(sid, b, p)
            cash -= b * p + f
            fees += f
            hold[sid] = b * Fraction(m)
            ind[industry_of[sid]] = ind.get(industry_of[sid], Fraction(0)) + b * Fraction(m)
            qty[sid] = b
    names_n = len(qty)
    stock_raw = sum((qty[s] * close[s] for s in qty), Decimal(0))
    ok = startup_pass(names_n, stock_raw, capital)
    return {"status": "STARTED" if ok else "NOT_STARTED_UNFIT", "names": names_n, "stock_value": stock_raw,
            "cash": cash, "fees": fees, "quantities": qty, "equity": cash + stock_raw,
            "reason": None if ok else ("STARTUP_UNFIT_NAMES" if names_n < STARTUP_MIN_NAMES else "STARTUP_UNFIT_RATIO")}


def startup_pass(names: int, stock_raw: Decimal, capital: Decimal) -> bool:
    """至少24只非零持仓且原始股票市值/入金本金≥50%，两者均含等号。"""
    return names >= STARTUP_MIN_NAMES and Fraction(stock_raw) >= STARTUP_MIN_STOCK_RATIO * Fraction(capital)


def check_sell_funding(proceeds: Decimal, costs: Decimal, free_cash: Decimal) -> str | None:
    """卖出税费若超过回款与可用现金：COST_UNFUNDABLE，不能虚构融资。"""
    return "COST_UNFUNDABLE" if costs > proceeds + max(free_cash, Decimal(0)) else None


def ordinary_sell_qty(grid: QuantityGrid, q: int, m: Decimal, target: Fraction, free_sell: int) -> int:
    """普通减持：不越过目标的最大合法差量 x，使 (q−x)m ≥ target；不足最小单位则0。"""
    excess = Fraction(q) * Fraction(m) - target
    if excess <= 0:
        return 0
    upper = int(excess / Fraction(m))
    return grid.max_sell(upper, free_sell, q)


NO_RETRY_REASONS = ("TARGET_SATISFIED", "TARGET_SHRUNK", "CAPITAL_CHANGED", "GLOBAL_BUY_BLOCK", "SECURITY_BUY_BLOCK",
                    "FLAT_SCORE", "WINDOW_END")


def retry_proposal(order, *, target_qty: int, grid: QuantityGrid, attempts_left: int, capital_revision: int,
                   decision: dict | None, cash_free: Decimal, fee_fn, free_sell: int | None = None) -> list[dict]:
    """终态后同一有效窗口内的剩余目标重试建议（仅建议/人工清单，不调用券商）。
    已知原单未结束、需对账、目标已满足或资本修订/禁买使意图失效时，不产生替代订单。"""
    if order.status not in ("FILLED", "CANCELED", "REJECTED"):
        return []
    reason = (order.cancel_reason or "").split(":")[0]
    if reason in NO_RETRY_REASONS or attempts_left <= 0 or order.capital_revision != capital_revision:
        return []
    if order.side == "BUY" and decision is not None and (decision["global_buy_blocks"]
                                                         or order.security_id in decision["security_buy_blocks"]):
        return []
    remaining = target_qty - order.filled_qty
    if remaining <= 0:
        return []
    if order.side == "BUY":
        q = grid.max_buy(remaining)
        if q == 0 or Decimal(q) * order.limit + fee_fn(q) > cash_free:
            return []
    else:
        fs = free_sell if free_sell is not None else remaining
        q = grid.max_sell(remaining, fs, fs)
        if q == 0:
            return []
    return [{"side": order.side, "qty": q, "limit": str(order.limit)}]
