"""因子研究共用：读取 2026-10-15 已整理的 Tushare 面板（只读）、按 V5D2 规则近似重建 400 池、计算候选因子。

数据：ETF 仓库 artifacts/stock_multifactor_20261015/panel（交易日 × 股票宽矩阵，临时 Tushare 来源）。
本模块只做研究诊断；生产因子实现见 smallcap/factor_impl.py（按 V5D2 合同用精确算术重写）。
"""
from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd

PANEL = Path("/home/wjc/src/github/etf/artifacts/stock_multifactor_20261015/panel")
PRIOR_RAW = Path("/home/wjc/src/github/etf/artifacts/stock_multifactor_20261015/raw")
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "research_out"
OUT.mkdir(exist_ok=True)

FIN_L1 = ("801780.SI", "801790.SI")          # 申万2021 银行、非银金融


def board_of(code: str) -> str:
    sym, ex = code.split(".")
    if ex == "SH":
        return "STAR" if sym.startswith("688") else "MAIN_SH"
    if ex == "SZ":
        return "CHINEXT" if sym.startswith(("300", "301")) else "MAIN_SZ"
    return "OTHER"


ALL_BOARDS = ("MAIN_SH", "MAIN_SZ", "CHINEXT", "STAR")


def mv_floor_yuan(date: str, board: str) -> float:
    """市值退市红线（单日检查，研究近似）：主板 2020-12-31 起 3 亿、2024-10-30 起 5 亿；
    创业板 2020-12-31 起 3 亿；科创板 2019-07-22 起 3 亿；此前不适用。"""
    if board == "STAR":
        return 3e8 if date >= "20190722" else 0.0
    if board == "CHINEXT":
        return 3e8 if date >= "20201231" else 0.0
    if date >= "20241030":
        return 5e8
    if date >= "20201231":
        return 3e8
    return 0.0


class Data:
    """宽矩阵（行=交易日，列=股票）。金额单位：amount 千元→元；市值 万元→元。"""

    def __init__(self):
        self.dates = np.load(PANEL / "dates.npy", allow_pickle=True).astype("U8")
        self.codes = np.load(PANEL / "codes.npy", allow_pickle=True).astype("U9")
        self.meta = pd.read_parquet(PANEL / "meta.parquet")
        self.n, self.m = len(self.dates), len(self.codes)
        self.board = np.array([board_of(c) for c in self.codes])
        cal = pd.read_parquet(PRIOR_RAW / "trade_cal.parquet")
        self.open_days = np.array(sorted(str(x) for x in cal.loc[cal.is_open == 1, "cal_date"]), dtype="U8")
        lst = np.array([str(x) if isinstance(x, str) and x else "99999999" for x in self.meta.list_date], dtype="U8")
        self.list_idx = np.searchsorted(self.open_days, lst) - np.searchsorted(self.open_days, self.dates[0])
        self.delist = np.array([str(x) if isinstance(x, str) else "" for x in self.meta.delist_date], dtype="U8")

    def a(self, name, dtype=np.float64):
        return np.load(PANEL / f"{name}.npy", mmap_mode="r")

    @cached_property
    def close(self):
        return np.load(PANEL / "close.npy")

    @cached_property
    def open(self):
        return np.load(PANEL / "open.npy")

    @cached_property
    def pre_close(self):
        return np.load(PANEL / "pre_close.npy")

    @cached_property
    def trading(self):
        return np.isfinite(self.close)

    @cached_property
    def ret(self):
        with np.errstate(invalid="ignore", divide="ignore"):
            r = self.close / self.pre_close - 1.0
        return np.where(self.trading, r, np.nan)

    @cached_property
    def amount(self):        # 元
        return np.load(PANEL / "amount.npy").astype(np.float64) * 1000.0

    @cached_property
    def mv(self):            # 元，真实日频总市值
        return np.load(PANEL / "total_mv_d.npy").astype(np.float64) * 10000.0

    @cached_property
    def st(self):
        return np.load(PANEL / "st.npy")

    @cached_property
    def limit(self):
        return np.load(PANEL / "limit.npy")

    @cached_property
    def turn(self):          # 自由流通换手率（小数）：vol(手)×100 / free_share(万股)×10000
        return np.load(PANEL / "turn_f.npy").astype(np.float64) / 100.0

    @cached_property
    def financial(self):
        """申万2021一级行业为银行/非银金融：按纳入/剔除日期；最早一条向前回填到上市日（近似历史）。"""
        sw = pd.read_parquet(RAW / "sw2021_l1_members.parquet")
        ci = pd.Index(self.codes)
        fin = np.zeros((self.n, self.m), dtype=bool)
        known = np.zeros((self.n, self.m), dtype=bool)
        sw = sw.sort_values(["ts_code", "in_date"])
        first = sw.groupby("ts_code").in_date.transform("min")
        for r in sw.itertuples():
            j = ci.get_indexer([r.ts_code])[0]
            if j < 0:
                continue
            a = 0 if r.in_date == first[r.Index] else np.searchsorted(self.dates, str(r.in_date))
            b = self.n if pd.isna(r.out_date) else np.searchsorted(self.dates, str(r.out_date), side="right")
            known[a:b, j] = True
            if r.l1_code in FIN_L1:
                fin[a:b, j] = True
        self.industry_known = known
        return fin

    @cached_property
    def industry(self):
        """申万一级代码（同上近似历史），未知为 ''。"""
        sw = pd.read_parquet(RAW / "sw2021_l1_members.parquet").sort_values(["ts_code", "in_date"])
        ci = pd.Index(self.codes)
        ind = np.full((self.n, self.m), "", dtype=object)
        first = sw.groupby("ts_code").in_date.transform("min")
        for r in sw.itertuples():
            j = ci.get_indexer([r.ts_code])[0]
            if j < 0:
                continue
            a = 0 if r.in_date == first[r.Index] else np.searchsorted(self.dates, str(r.in_date))
            b = self.n if pd.isna(r.out_date) else np.searchsorted(self.dates, str(r.out_date), side="right")
            ind[a:b, j] = r.l1_code
        return ind

    def week_ends(self, start="20101231"):
        dt = pd.to_datetime(pd.Series(self.dates))
        key = dt.dt.strftime("%G%V")
        idx = pd.Series(range(len(dt))).groupby(key).max().sort_values().to_numpy()
        return idx[(self.dates[idx] >= start) & (idx < self.n - 1)]

    def rolling(self, x, w, fn, minp):
        return getattr(pd.DataFrame(x).rolling(w, min_periods=minp), fn)().to_numpy()

    def adv20(self):
        """含当日的最近 20 个市场日成交额均值；停牌日计 0（上市期内无交易视为确认停牌）。"""
        listed = (np.arange(self.n)[:, None] >= self.list_idx[None, :])
        amt = np.where(self.trading, self.amount, np.where(listed, 0.0, np.nan))
        return self.rolling(amt, 20, "mean", 20)

    def pool_masks(self, t_idx, adv20, limit=400, boards=ALL_BOARDS):
        """V5D2 近似 400 池：返回 (eligible_mask, pool_mask) 两个 [len(t_idx), m] 布尔矩阵。"""
        fin = self.financial
        elig = np.zeros((len(t_idx), self.m), dtype=bool)
        pool = np.zeros_like(elig)
        bmask = np.isin(self.board, boards)
        for k, t in enumerate(t_idx):
            fl = {b: mv_floor_yuan(self.dates[t], b) for b in ALL_BOARDS}
            floor = np.array([fl.get(b, 0.0) for b in self.board])
            e = (self.trading[t] & bmask & ~self.st[t] & (t - self.list_idx >= 250) & (self.close[t] >= 2.0)
                 & (adv20[t] >= 1e7) & np.isfinite(self.mv[t]) & (self.mv[t] >= floor) & ~fin[t])
            elig[k] = e
            idx = np.flatnonzero(e)
            order = idx[np.lexsort((self.codes[idx], self.mv[t, idx]))]
            pool[k, order[:limit]] = True
        return elig, pool

    @cached_property
    def cumlr(self):
        """收盘到收盘对数总收益的累计（停牌日为 0）。"""
        return np.cumsum(np.log1p(np.where(self.trading, self.ret, 0.0)), axis=0)

    def open_to_open(self, e, x):
        """执行日 e 开盘买入 → x 日开盘的简单收益（含分红，昨收参考价链）；e 日不交易为 NaN。"""
        with np.errstate(invalid="ignore", divide="ignore"):
            first = np.log(self.close[e] / self.open[e])
            mid = self.cumlr[x - 1] - self.cumlr[e]
            last = np.where(self.trading[x], np.log(self.open[x] / self.pre_close[x]), 0.0)
        r = np.exp(first + mid + last) - 1.0
        r[~self.trading[e]] = np.nan
        return r


def _first_disclosed(t: pd.DataFrame, ann_col: str) -> pd.DataFrame:
    t = t.dropna(subset=[ann_col]).copy()
    t[ann_col] = t[ann_col].astype(str)
    t["_uf"] = t.update_flag.astype(str)
    return t.sort_values(["ts_code", "end_date", ann_col, "_uf"], kind="mergesort").drop_duplicates(
        ["ts_code", "end_date"], keep="first")


def ttm_np(data: Data, t_idx):
    """归母净利润 TTM（按公告日 PIT；同一报告期取首次披露；报告期须在 15 个月内）。返回 [len(t_idx), m]。"""
    inc = pd.read_parquet(PRIOR_RAW / "income.parquet")
    inc = inc[inc.report_type.astype(str) == "1"].copy()
    inc["ann"] = inc.f_ann_date.fillna(inc.ann_date)
    inc = _first_disclosed(inc.dropna(subset=["n_income_attr_p"]), "ann")
    inc = inc[inc.end_date.str[4:].isin(["0331", "0630", "0930", "1231"])]
    ci = pd.Index(data.codes)
    cut = data.dates[t_idx]
    stale = np.array([(pd.Timestamp(str(c)) - pd.DateOffset(months=15)).strftime("%Y%m%d") for c in cut])
    out = np.full((len(t_idx), data.m), np.nan)
    for code, g in inc.groupby("ts_code", sort=False):
        j = ci.get_indexer([code])[0]
        if j < 0:
            continue
        g = g.sort_values("ann", kind="mergesort")
        anns, ends, vals = g.ann.to_numpy(), g.end_date.to_numpy(), g.n_income_attr_p.to_numpy(float)
        have: dict = {}
        p = 0
        for k, c in enumerate(cut):
            while p < len(anns) and anns[p] <= c:
                have[ends[p]] = vals[p]
                p += 1
            if not have:
                continue
            last = max(have)
            if last < stale[k]:
                continue
            y, md = last[:4], last[4:]
            if md == "1231":
                out[k, j] = have[last]
                continue
            fy, same = f"{int(y) - 1}1231", f"{int(y) - 1}{md}"
            if fy in have and same in have:
                out[k, j] = have[last] + have[fy] - have[same]
    return out


def pit_field(data: Data, t_idx, table: str, col: str):
    """fina_indicator 等表：截止日已公告的最新报告期取值（同期取首次披露），15 个月有效。"""
    t = _first_disclosed(pd.read_parquet(PRIOR_RAW / f"{table}.parquet"), "ann_date")
    ci = pd.Index(data.codes)
    cut = data.dates[t_idx]
    stale = np.array([(pd.Timestamp(str(c)) - pd.DateOffset(months=15)).strftime("%Y%m%d") for c in cut])
    out = np.full((len(t_idx), data.m), np.nan)
    for code, g in t.groupby("ts_code", sort=False):
        j = ci.get_indexer([code])[0]
        if j < 0:
            continue
        g = g.sort_values("ann_date", kind="mergesort")
        anns, ends, vals = g.ann_date.to_numpy(), g.end_date.to_numpy(), g[col].to_numpy(float)
        have: dict = {}
        p = 0
        for k, c in enumerate(cut):
            while p < len(anns) and anns[p] <= c:
                have[ends[p]] = vals[p]
                p += 1
            if have:
                last = max(have)
                if last >= stale[k]:
                    out[k, j] = have[last]
    return out
