"""投资候选因子的已登记纯函数实现（H03.2：公式号 + 已登记函数，禁止 eval 配置表达式）。

每个实现只读 facts[sid] 中按名提供的已裁决输入 (value, status, available_at)，输出 FactorValue；
数值用 50 位 Decimal 计算后由 compute_factor 按包内 output_quantum 量化（HALF_UP），保证可复现。
状态约定（D05.4、H03.3）：
- 输入缺失/来源未知 → SOURCE_UNKNOWN（走两健康周规则）；
- 已确认分母非法、必要观测不足 → MODEL_INAPPLICABLE（健康周内普通退出）；
- 报告期超过有效期 → STALE；
- 合法负利润保留为负值（不改为 0、不记为缺失）。
available_at 晚于截止时刻的输入一律视为未来依赖（SOURCE_UNKNOWN），不得使用。
"""
from __future__ import annotations

import decimal
from datetime import datetime
from decimal import Decimal

from .factors import (MODEL_INAPPLICABLE, REGISTRY, SOURCE_UNKNOWN, STALE, VALID, FactorValue)

MIN_WINDOW_OBS = 40          # 60 日窗口至少 40 个有效交易日（预登记）
WINDOW = 60


def _ctx():
    c = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN)
    return c


def _get(facts: dict, sid: str, field: str, cutoff: datetime):
    """取一个输入；返回 (value, None) 或 (None, FactorValue 失败原因)。"""
    rec = facts.get(sid, {}).get(field)
    if rec is None:
        return None, FactorValue(None, SOURCE_UNKNOWN, f"{field}_MISSING")
    value, status, avail = rec
    if avail is not None and avail > cutoff:
        return None, FactorValue(None, SOURCE_UNKNOWN, f"{field}_FUTURE_DEPENDENCY", avail)
    if status != VALID:
        return None, FactorValue(None, status, f"{field}_{status}", avail)
    if value is None:
        return None, FactorValue(None, SOURCE_UNKNOWN, f"{field}_MISSING", avail)
    return (value, avail), None


def _maxavail(*xs):
    vals = [x for x in xs if x is not None]
    return max(vals) if vals else None


def size_total_mv(spec: dict, pool_ids: list, cutoff: datetime, facts: dict) -> dict:
    """SIZE：总市值（元）= 当日总股本×未复权收盘价；方向 LOWER_BETTER（越小越好）。MV≤0 → MODEL_INAPPLICABLE。"""
    out = {}
    for sid in pool_ids:
        got, bad = _get(facts, sid, "market_value_total", cutoff)
        if bad:
            out[sid] = bad
            continue
        mv, avail = got
        if mv <= 0:
            out[sid] = FactorValue(None, MODEL_INAPPLICABLE, "MV_NONPOSITIVE", avail)
            continue
        out[sid] = FactorValue(Decimal(mv), VALID, "", avail)
    return out


def ep_ttm_parent(spec: dict, pool_ids: list, cutoff: datetime, facts: dict) -> dict:
    """EP：归母净利润 TTM ÷ 总市值；TTM=当期累计+上年全年−上年同期累计（Q4 用全年），按公告可用时刻 as-of。
    亏损保留为负值；MV≤0 → MODEL_INAPPLICABLE；TTM 依赖缺失 → SOURCE_UNKNOWN；报告期超 15 个月 → STALE（由输入状态给出）。"""
    out = {}
    c = _ctx()
    for sid in pool_ids:
        np_, bad = _get(facts, sid, "np_parent_ttm", cutoff)
        if bad:
            out[sid] = bad
            continue
        mv_, bad = _get(facts, sid, "market_value_total", cutoff)
        if bad:
            out[sid] = bad
            continue
        (ni, a1), (mv, a2) = np_, mv_
        if mv <= 0:
            out[sid] = FactorValue(None, MODEL_INAPPLICABLE, "MV_NONPOSITIVE", _maxavail(a1, a2))
            continue
        out[sid] = FactorValue(c.divide(Decimal(ni), Decimal(mv)), VALID, "", _maxavail(a1, a2))
    return out


def _window(facts, sid, field, cutoff):
    got, bad = _get(facts, sid, field, cutoff)
    if bad:
        return None, None, bad
    win, avail = got
    if len(win) != WINDOW:
        return None, None, FactorValue(None, SOURCE_UNKNOWN, f"{field}_WINDOW_LENGTH_{len(win)}", avail)
    return win, avail, None


def vol60_daily_return_std(spec: dict, pool_ids: list, cutoff: datetime, facts: dict) -> dict:
    """VOL60：最近 60 个市场日中各有效交易日的日收益 r=收盘/交易所参考昨收−1 的样本标准差（n−1），
    至少 40 个有效日，否则 MODEL_INAPPLICABLE；方向 LOWER_BETTER。窗口元素为 (close, ref_preclose) 或 None（停牌）。"""
    out = {}
    c = _ctx()
    for sid in pool_ids:
        win, avail, bad = _window(facts, sid, "daily_return_window_60", cutoff)
        if bad:
            out[sid] = bad
            continue
        rs = []
        for x in win:
            if x is None:
                continue
            close, ref = Decimal(x[0]), Decimal(x[1])
            if ref <= 0 or close <= 0:
                rs = None
                break
            rs.append(c.subtract(c.divide(close, ref), 1))
        if rs is None:
            out[sid] = FactorValue(None, MODEL_INAPPLICABLE, "NONPOSITIVE_PRICE", avail)
            continue
        if len(rs) < MIN_WINDOW_OBS:
            out[sid] = FactorValue(None, MODEL_INAPPLICABLE, f"INSUFFICIENT_TRADING_DAYS_{len(rs)}", avail)
            continue
        n = len(rs)
        mean = c.divide(sum(rs, Decimal(0)), n)
        ss = sum((c.multiply(r - mean, r - mean) for r in rs), Decimal(0))
        out[sid] = FactorValue(c.sqrt(c.divide(ss, n - 1)), VALID, "", avail)
    return out


def turn60_free_float(spec: dict, pool_ids: list, cutoff: datetime, facts: dict) -> dict:
    """TURN60：最近 60 个市场日中各有效交易日的自由流通换手率 成交股数÷自由流通股数 的算术平均，
    至少 40 个有效日，否则 MODEL_INAPPLICABLE；自由流通股数≤0 → MODEL_INAPPLICABLE；方向 LOWER_BETTER。
    窗口元素为 (volume_shares, free_float_shares) 或 None（停牌）。"""
    out = {}
    c = _ctx()
    for sid in pool_ids:
        win, avail, bad = _window(facts, sid, "turnover_window_60", cutoff)
        if bad:
            out[sid] = bad
            continue
        ts = []
        for x in win:
            if x is None:
                continue
            vol, free = Decimal(x[0]), Decimal(x[1])
            if free <= 0 or vol < 0:
                ts = None
                break
            ts.append(c.divide(vol, free))
        if ts is None:
            out[sid] = FactorValue(None, MODEL_INAPPLICABLE, "FREE_FLOAT_NONPOSITIVE", avail)
            continue
        if len(ts) < MIN_WINDOW_OBS:
            out[sid] = FactorValue(None, MODEL_INAPPLICABLE, f"INSUFFICIENT_TRADING_DAYS_{len(ts)}", avail)
            continue
        out[sid] = FactorValue(c.divide(sum(ts, Decimal(0)), len(ts)), VALID, "", avail)
    return out


IMPLEMENTATIONS = {
    "SIZE_TOTAL_MV_V1": size_total_mv,
    "EP_TTM_PARENT_V1": ep_ttm_parent,
    "VOL60_DAILY_RETURN_STD_V1": vol60_daily_return_std,
    "TURN60_FREE_FLOAT_V1": turn60_free_float,
}

# 每个实现所需输入字段（行情适配器据此提供 as-of 事实）
INPUT_FIELDS = {
    "SIZE_TOTAL_MV_V1": ("market_value_total",),
    "EP_TTM_PARENT_V1": ("np_parent_ttm", "market_value_total"),
    "VOL60_DAILY_RETURN_STD_V1": ("daily_return_window_60",),
    "TURN60_FREE_FLOAT_V1": ("turnover_window_60",),
}

REGISTRY.update(IMPLEMENTATIONS)
