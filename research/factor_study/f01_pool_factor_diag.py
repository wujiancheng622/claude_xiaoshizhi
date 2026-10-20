"""单因子诊断（描述性，按预登记第 4.1 节）：在 V5D2 近似 400 池内的周度秩 IC、前 30 只超额、换手、池内市值名次、崩盘窗口。

输出：research_out/weekly_panel.npz（池、因子、下周收益，供后续复用）与 research_out/f01_diag.json / f01_diag.md。
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from common import OUT, Data, pit_field, ttm_np

SIGN = {"SIZE": -1, "EP": +1, "BP": +1, "VOL60": -1, "TURN60": -1, "REV20": -1, "MAX20": -1, "DY": +1, "ROE": +1}
PERIODS = {"2011-2018": ("20110101", "20181231"), "2019-2026": ("20190101", "20261231"), "全期": ("20110101", "20261231")}
CRASH = {"2015-06~09 股灾": ("20150612", "20150915"), "2016-01 熔断": ("20160101", "20160129"),
         "2018 全年": ("20180101", "20181231"), "2024-01~02 微盘踩踏": ("20240101", "20240207"),
         "2024-04 国九条": ("20240408", "20240419")}


def build():
    D = Data()
    t_idx = D.week_ends("20101231")
    print("weeks", len(t_idx), D.dates[t_idx[0]], D.dates[t_idx[-1]], flush=True)
    adv = D.adv20()
    elig, pool = D.pool_masks(t_idx, adv)
    print("pool size median", int(np.median(pool.sum(1))), "min", int(pool.sum(1).min()), flush=True)
    F = {}
    mv = D.mv[t_idx]
    F["SIZE"] = np.log(mv)
    F["EP"] = ttm_np(D, t_idx) / mv
    pb = np.load("/home/wjc/src/github/etf/artifacts/stock_multifactor_20261015/panel/pb.npy")[t_idx]
    with np.errstate(divide="ignore", invalid="ignore"):
        F["BP"] = np.where(np.isfinite(pb) & (pb != 0), 1.0 / pb, np.nan)
    F["VOL60"] = D.rolling(D.ret, 60, "std", 40)[t_idx]
    turn = np.where(D.trading, D.turn, np.nan)
    F["TURN60"] = D.rolling(turn, 60, "mean", 40)[t_idx]
    cl = D.cumlr
    F["REV20"] = np.exp(cl[t_idx] - cl[t_idx - 20]) - 1.0
    F["MAX20"] = D.rolling(D.ret, 20, "max", 10)[t_idx]
    dy = np.load("/home/wjc/src/github/etf/artifacts/stock_multifactor_20261015/panel/dv_ttm.npy")[t_idx]
    F["DY"] = np.where(np.isfinite(mv), np.nan_to_num(dy, nan=0.0), np.nan)
    F["ROE"] = pit_field(D, t_idx, "fina_indicator", "roe_yearly")
    # 下周收益：执行日 e=t+1 开盘 → 下一执行日开盘
    fwd = np.full((len(t_idx), D.m), np.nan)
    for k in range(len(t_idx) - 1):
        fwd[k] = D.open_to_open(t_idx[k] + 1, t_idx[k + 1] + 1)
    size_rank = np.full((len(t_idx), D.m), np.nan)
    for k in range(len(t_idx)):
        idx = np.flatnonzero(pool[k])
        size_rank[k, idx[np.argsort(mv[k, idx])]] = np.arange(1, len(idx) + 1)
    np.savez_compressed(OUT / "weekly_panel.npz", t_idx=t_idx, dates=D.dates[t_idx], pool=pool, elig=elig, fwd=fwd,
                        size_rank=size_rank, codes=D.codes, **{k: v.astype(np.float64) for k, v in F.items()})
    return D.dates[t_idx], pool, fwd, size_rank, F


def spearman(a, b):
    ra, rb = pd.Series(a).rank().to_numpy(), pd.Series(b).rank().to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def diag(dates, pool, fwd, size_rank, F, top=30):
    res = {}
    for name, x in F.items():
        s = SIGN[name]
        rows = []
        prev_top = set()
        for k in range(len(dates) - 1):
            v = pool[k] & np.isfinite(x[k]) & np.isfinite(fwd[k])
            cov = float((pool[k] & np.isfinite(x[k])).sum() / max(pool[k].sum(), 1))
            if v.sum() < 50:
                rows.append((dates[k], np.nan, np.nan, np.nan, np.nan, np.nan, cov))
                continue
            sc, fr = s * x[k][v], fwd[k][v]
            ic = spearman(sc, fr)
            order = np.argsort(-sc, kind="mergesort")
            idx = np.flatnonzero(v)
            topi, boti = idx[order[:top]], idx[order[-top:]]
            pool_ret = float(np.mean(fr))
            ex_top = float(np.mean(fwd[k][topi]) - pool_ret)
            ex_bot = float(np.mean(fwd[k][boti]) - pool_ret)
            ts = set(topi.tolist())
            to = (len(ts - prev_top) / top) if prev_top else np.nan
            prev_top = ts
            rows.append((dates[k], ic, ex_top, ex_bot, float(np.nanmedian(size_rank[k][topi])), to, cov))
        df = pd.DataFrame(rows, columns=["date", "ic", "ex_top", "ex_bot", "top_size_rank", "top_turnover", "coverage"])
        out = {}
        for pname, (a, b) in PERIODS.items():
            g = df[(df.date >= a) & (df.date <= b)].dropna(subset=["ic"])
            ic = g.ic
            out[pname] = {"weeks": int(len(g)), "ic_mean": float(ic.mean()), "ic_t": float(ic.mean() / ic.std() * np.sqrt(len(ic))),
                          "ex_top_ann": float(g.ex_top.mean() * 52), "ex_bot_ann": float(g.ex_bot.mean() * 52),
                          "top_size_rank": float(g.top_size_rank.median()), "top_turnover": float(g.top_turnover.mean()),
                          "coverage_min": float(df[(df.date >= a) & (df.date <= b)].coverage.min()),
                          "coverage_mean": float(df[(df.date >= a) & (df.date <= b)].coverage.mean())}
        for cname, (a, b) in CRASH.items():
            g = df[(df.date >= a) & (df.date <= b)]
            out[cname] = {"ex_top_sum": float(g.ex_top.sum()), "ex_bot_sum": float(g.ex_bot.sum()), "weeks": int(len(g))}
        res[name] = out
    # 池等权与"最小30只"在崩盘窗口的累计（基准参照）
    ref = {}
    for cname, (a, b) in CRASH.items():
        ks = [k for k in range(len(dates) - 1) if a <= dates[k] <= b]
        pool_c = np.prod([1 + np.nanmean(fwd[k][pool[k] & np.isfinite(fwd[k])]) for k in ks]) - 1 if ks else np.nan
        small_c = np.prod([1 + np.nanmean(fwd[k][(size_rank[k] <= 30) & np.isfinite(fwd[k])]) for k in ks]) - 1 if ks else np.nan
        ref[cname] = {"pool_ew": float(pool_c), "smallest30": float(small_c)}
    return res, ref


def corr_table(dates, pool, F):
    names = list(F)
    acc = np.zeros((len(names), len(names)))
    n = 0
    for k in range(0, len(dates), 4):
        v = pool[k].copy()
        for x in F.values():
            v &= np.isfinite(x[k])
        if v.sum() < 50:
            continue
        R = np.array([pd.Series(SIGN[nm] * F[nm][k][v]).rank().to_numpy() for nm in names])
        acc += np.corrcoef(R)
        n += 1
    return pd.DataFrame(acc / n, index=names, columns=names)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "reuse":
        z = np.load(OUT / "weekly_panel.npz", allow_pickle=True)
        dates, pool, fwd, size_rank = z["dates"], z["pool"], z["fwd"], z["size_rank"]
        F = {k: z[k] for k in SIGN}
    else:
        dates, pool, fwd, size_rank, F = build()
    res, ref = diag(dates, pool, fwd, size_rank, F)
    corr = corr_table(dates, pool, F)
    (OUT / "f01_diag.json").write_text(json.dumps({"factors": res, "crash_ref": ref, "corr": corr.round(3).to_dict()},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# 单因子诊断（V5D2 近似 400 池，周度；描述性）", "",
             "IC=因子（已按方向取向）与下周开盘到开盘收益的秩相关；超额=前/后 30 只等权相对池等权的周均×52（未扣成本）。", ""]
    for pname in PERIODS:
        lines += [f"## {pname}", "", "| 因子 | 周数 | IC | t | 前30超额/年 | 后30超额/年 | 前30池内市值名次中位 | 前30周换手 | 覆盖率(均/最低) |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for nm, o in res.items():
            q = o[pname]
            lines.append(f"| {nm} | {q['weeks']} | {q['ic_mean']:+.4f} | {q['ic_t']:+.1f} | {q['ex_top_ann']:+.1%} | "
                         f"{q['ex_bot_ann']:+.1%} | {q['top_size_rank']:.0f} | {q['top_turnover']:.0%} | "
                         f"{q['coverage_mean']:.0%}/{q['coverage_min']:.0%} |")
        lines.append("")
    lines += ["## 崩盘窗口（前/后 30 只相对池等权的累计超额，周超额求和）", "",
              "| 因子 | " + " | ".join(CRASH) + " |", "|---|" + "---|" * len(CRASH)]
    for nm, o in res.items():
        lines.append(f"| {nm} | " + " | ".join(f"{o[c]['ex_top_sum']:+.1%} / {o[c]['ex_bot_sum']:+.1%}" for c in CRASH) + " |")
    lines += ["", "| 参照 | " + " | ".join(CRASH) + " |", "|---|" + "---|" * len(CRASH),
              "| 池等权累计 | " + " | ".join(f"{ref[c]['pool_ew']:+.1%}" for c in CRASH) + " |",
              "| 最小30只累计 | " + " | ".join(f"{ref[c]['smallest30']:+.1%}" for c in CRASH) + " |", "",
              "## 因子取向后的平均截面秩相关（每 4 周抽样）", "", corr.round(2).to_markdown(), ""]
    (OUT / "f01_diag.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
