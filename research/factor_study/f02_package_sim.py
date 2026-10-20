"""包级快速近似（按预登记第 3 节的 4 个候选包 + B0 + 参照），用 V5D2 主要机制做价值记账账户模拟。

只用于在搭好生产引擎回放之前先看量级；正式结论以生产引擎（smallcap 回放）为准。
近似：价值记账（分红随总收益再投资）、整手向中心取整、09:30 开盘±滑点成交、开盘涨停买不到/跌停卖不掉/停牌冻结、
单股>5%或总仓>65%次日按比例纠偏、ST/价格<2 次日退出、日终回撤≥25% 永久停用并逐步卖出；
行业 20% 与 S2/S3 情景不在此近似内（引擎里有）。
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from common import OUT, Data

PKGS = {
    "B0": ["SIZE"],
    "P1_SIZE_EP_VOL": ["SIZE", "EP", "VOL60"],
    "P2_SIZE_EP_TURN": ["SIZE", "EP", "TURN60"],
    "P3_EP_VOL_TURN": ["EP", "VOL60", "TURN60"],
    "P4_SIZE_EP_VOL_TURN": ["SIZE", "EP", "VOL60", "TURN60"],
    "R5_FIVE_STYLE_REF": ["EP", "BP", "ROE", "VOL60", "TURN60", "REV20"],
}
SIGN = {"SIZE": -1, "EP": +1, "BP": +1, "VOL60": -1, "TURN60": -1, "REV20": -1, "ROE": +1}
STARTS = {"A0": "2011", "A1": "2016", "A2": "2021", "A3": "2024"}


def pct_score(x):
    """平均并列百分位映射到 [-1,1]：z=(a+b−n−1)/(n−1)。"""
    n = len(x)
    if n == 1:
        return np.zeros(1)
    r = pd.Series(x).rank(method="average").to_numpy()          # 平均名次 (a+b)/2
    return (2 * r - n - 1) / (n - 1)


def scores(z, k, factors):
    pool = z["pool"][k]
    v = pool.copy()
    for f in factors:
        v &= np.isfinite(z[f][k])
    idx = np.flatnonzero(v)
    s = np.zeros(len(idx))
    for f in factors:
        s += pct_score(SIGN[f] * z[f][k][idx])
    s /= len(factors)
    return idx, s, v.sum() / max(pool.sum(), 1)


def run(D, z, factors, start_year, slip=0.001, capital=500000.0, retain=45, end="20261014", center=0.02, retire_dd=0.25):
    dates, t_idx = z["dates"], z["t_idx"]
    codes = z["codes"]
    ks = [k for k in range(len(dates)) if dates[k][:4] == start_year]
    # 起点：该年第一个完整周（周一也在当年）的最后市场日
    k0 = next(k for k in ks if pd.Timestamp(str(dates[k])) - pd.Timedelta(days=pd.Timestamp(str(dates[k])).weekday())
              >= pd.Timestamp(f"{start_year}-01-01"))
    o, c, pc, lim, st = D.open, D.close, D.pre_close, D.limit, D.st
    tr = D.trading
    m = D.m
    val = np.zeros(m)                  # 持仓市值（按最近收盘）
    cash = capital
    nav = []
    hwm, retired = capital, False
    t_start = t_idx[k0]
    t_end = int(np.searchsorted(D.dates, end, side="right")) - 1
    week_of = {t_idx[k] + 1: k for k in range(len(t_idx))}      # 执行日 → 计划周
    plan = None
    unscorable = np.zeros(m, dtype=int)
    fees_total, trades = 0.0, 0
    pending_reduce = None
    covs = []
    for t in range(t_start, t_end + 1):
        if t > t_start:
            # 开盘：价值按开盘缺口调整
            gap = np.where(tr[t] & (val > 0), o[t] / pc[t], 1.0)
            gap = np.where(np.isfinite(gap), gap, 1.0)
            val = val * gap
            E = cash + val.sum()
            up = np.round(pc[t] * (1 + lim[t]), 2)
            dn = np.round(pc[t] * (1 - lim[t]), 2)
            can_buy = tr[t] & (o[t] < up - 0.005)
            can_sell = tr[t] & (o[t] > dn + 0.005)
            sells = np.zeros(m)
            buys = np.zeros(m)
            if retired:
                sells = np.where(can_sell, val, 0.0)
            else:
                # 风险退出与纠偏（上一日终确定）
                if pending_reduce is not None:
                    sells = np.maximum(sells, np.where(can_sell, pending_reduce, 0.0))
                    pending_reduce = None
                exit_risk = (val > 0) & (st[t - 1] | (c[t - 1] < 2.0))
                sells = np.where(exit_risk & can_sell, val, sells)
                if t in week_of and plan is not None:
                    tgt_val, E_dec, keep_band, buy_list = plan
                    target = center * E_dec
                    held = val > 0
                    for j in np.flatnonzero(held & ~exit_risk):
                        if j in tgt_val:
                            w = val[j] / E_dec
                            if 0.75 * center <= w <= 1.25 * center:
                                continue
                            dv = target - val[j]
                            px = o[t, j] * (1 + np.sign(dv) * slip)
                            lots = np.floor(abs(dv) / (px * 100))
                            if lots >= 1:
                                if dv > 0 and can_buy[j]:
                                    buys[j] = lots * 100 * px
                                elif dv < 0 and can_sell[j]:
                                    sells[j] = max(sells[j], min(lots * 100 * px, val[j]))
                        elif can_sell[j]:
                            sells[j] = val[j]
                    nheld = int((held & ~((sells >= val) & held)).sum())
                    frozen = int((held & ~can_sell & ~np.isin(np.arange(m), list(tgt_val))).sum())
                    slots = 30 - max(nheld, 0)
                    for j in buy_list:
                        if slots <= 0:
                            break
                        if val[j] > 0 or not can_buy[j]:
                            continue
                        px = o[t, j] * (1 + slip)
                        lots = np.floor(target / (px * 100))
                        if lots >= 1:
                            buys[j] = lots * 100 * px
                            slots -= 1
            # 成交：先卖后买，费用
            sell_amt = np.minimum(sells, val)
            for j in np.flatnonzero(sell_amt > 0):
                a = sell_amt[j]
                fee = max(5.0, 0.00025 * a) + (0.0005 if D.dates[t] >= "20230828" else 0.001) * a + 0.00001 * a
                proceeds = a * (1 - slip)
                cash += proceeds - fee
                val[j] -= a
                fees_total += fee + a * slip
                trades += 1
            for j in np.flatnonzero(buys > 0):
                a = buys[j]
                fee = max(5.0, 0.00025 * a) + 0.00001 * a
                if a + fee > cash:
                    continue
                cash -= a + fee
                val[j] += a / (1 + slip)                  # 以含滑点价格买入，按开盘价计值
                fees_total += fee + a * slip / (1 + slip)
                trades += 1
            val[val < 1e-6] = 0.0
            # 开盘→收盘
            r = np.where(tr[t] & (val > 0), c[t] / o[t], 1.0)
            val = val * np.where(np.isfinite(r), r, 1.0)
            # 退市：到最后交易日仍持有记零
            dead = (val > 0) & (D.delist != "") & (np.array([False]) if False else (D.delist <= D.dates[t]))
            val[dead] = 0.0
        E = cash + val.sum()
        nav.append((D.dates[t], E / capital))
        hwm = max(hwm, E)
        if not retired and retire_dd is not None and E <= (1 - retire_dd) * hwm:
            retired = True
        if not retired:
            # 日终风险：单股>5%→降到2%，总仓>65%→降到60%
            red = np.where(val > 0.05 * E, val - center * E, 0.0)
            tot = val.sum()
            if tot > 30 * center * 65 / 60 * E:
                red = np.maximum(red, val * (1 - 30 * center * E / tot))
            pending_reduce = red if red.any() else None
        # 周度截止：形成计划
        k = int(np.searchsorted(t_idx, t))
        if k < len(t_idx) and t_idx[k] == t and not retired:
            if factors == ["SIZE"]:
                idx = np.flatnonzero(z["pool"][k])
                s = -np.log(z["SIZE"][k][idx] * 0 + np.exp(z["SIZE"][k][idx]))
                cov = 1.0
            else:
                idx, s, cov = scores(z, k, factors)
            covs.append(cov)
            order = idx[np.lexsort((codes[idx], -s))]
            rank = {j: i + 1 for i, j in enumerate(order)}
            held = np.flatnonzero(val > 0)
            keep = {}
            for j in held:
                if j in rank and rank[j] <= retain:
                    keep[j] = True
                    unscorable[j] = 0
                elif z["pool"][k][j] and j not in rank and factors != ["SIZE"]:
                    unscorable[j] += 1
                    if unscorable[j] < 3:
                        keep[j] = True                 # 暂缺不足两个健康周：HOLD_NO_ADD
            buy_list = [j for j in order if val[j] <= 0][:60]
            plan = (keep, E, None, buy_list) if cov >= 0.8 else (dict.fromkeys(held, True), E, None, [])
    s = pd.Series([v for _, v in nav], index=pd.to_datetime([d for d, _ in nav]))
    yrs = (s.index[-1] - s.index[0]).days / 365.2425
    mdd = float((1 - s / s.cummax()).max())
    return {"cagr": float(s.iloc[-1] ** (1 / yrs) - 1), "mdd": mdd, "retired": retired,
            "retire_date": str(s[(1 - s / s.cummax()) >= 0.25].index[0].date()) if retired else None,
            "fees_per_year": fees_total / capital / yrs, "trades_per_year": trades / yrs,
            "coverage_min": float(min(covs)) if covs else None, "nav_end": float(s.iloc[-1])}


def whatif():
    """非 V5D2：股票目标仓位敏感度（只供用户裁决参考）。"""
    D = Data()
    z = dict(np.load(OUT / "weekly_panel.npz", allow_pickle=True))
    res = {}
    for name in ("B0", "P1_SIZE_EP_VOL", "P3_EP_VOL_TURN"):
        for center in (0.02, 0.0167, 0.0133, 0.01):
            for sid, y in STARTS.items():
                r = run(D, z, PKGS[name], y, slip=0.001, center=center)
                res[f"{name}|{30*center:.0%}|{sid}"] = r
                print(name, f"{30*center:.0%}", sid, round(r["cagr"], 4), round(r["mdd"], 4), r["retired"], r["retire_date"], flush=True)
    (OUT / "f02_whatif_exposure.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


def grid():
    """非 V5D2：仓位 × 不设停用 → 各起点最大回撤与年化（S0、S1），供用户定仓位与停用线。"""
    D = Data()
    z = dict(np.load(OUT / "weekly_panel.npz", allow_pickle=True))
    res = {}
    for name in ("B0", "P1_SIZE_EP_VOL", "P2_SIZE_EP_TURN", "P3_EP_VOL_TURN", "P4_SIZE_EP_VOL_TURN"):
        for expo in (0.4, 0.5, 0.6, 0.8):
            for sid, y in STARTS.items():
                for sc, slip in (("S0", 0.001), ("S1", 0.003)):
                    r = run(D, z, PKGS[name], y, slip=slip, center=expo / 30, retire_dd=None)
                    res[f"{name}|{expo}|{sid}|{sc}"] = r
            print(name, expo, flush=True)
    (OUT / "f02_grid.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    D = Data()
    z = dict(np.load(OUT / "weekly_panel.npz", allow_pickle=True))
    for key in ("SIZE", "EP", "BP", "VOL60", "TURN60", "REV20", "ROE"):
        z[key] = z[key]
    res = {}
    for name, factors in PKGS.items():
        for sid, y in STARTS.items():
            for sc, slip in (("S0", 0.001), ("S1", 0.003)):
                for role, retain in (("B1", 45),) if name != "B0" else (("B0", 45),):
                    r = run(D, z, factors, y, slip=slip, retain=retain)
                    res[f"{name}|{sid}|{sc}"] = r
                    print(name, sid, sc, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}, flush=True)
    (OUT / "f02_package_sim.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    {"whatif": whatif, "grid": grid}.get(sys.argv[1] if len(sys.argv) > 1 else "", main)()
