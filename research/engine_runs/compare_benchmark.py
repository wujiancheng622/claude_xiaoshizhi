"""各包（S0、50万）与 ETF 基准在共同存续期内比较：策略净值 vs 60% ETF+40% 现金（每日再平衡近似）与 100% ETF。"""
import csv
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
d = pd.read_parquet(ROOT / "data/raw/bench_etf_daily.parquet").drop_duplicates(["ts_code", "trade_date"])
a = pd.read_parquet(ROOT / "data/raw/bench_etf_adj.parquet").drop_duplicates(["ts_code", "trade_date"])
x = d.merge(a[["ts_code", "trade_date", "adj_factor"]], on=["ts_code", "trade_date"], how="left").sort_values("trade_date")
x["adj_factor"] = x.groupby("ts_code").adj_factor.ffill()
ETF = {c: pd.Series((g.close * g.adj_factor).values, index=pd.to_datetime(g.trade_date)) for c, g in x.groupby("ts_code")}


def nav(cell_dir):
    rows = list(csv.DictReader(open(cell_dir / "ledger.csv")))
    return pd.Series([float(r["nav"]) for r in rows if r["nav"]],
                     index=pd.to_datetime([r["date"] for r in rows if r["nav"]]))


def stats(s):
    y = (s.index[-1] - s.index[0]).days / 365.2425
    return (s.iloc[-1] / s.iloc[0]) ** (1 / y) - 1, float((1 - s / s.cummax()).max())


out = []
for run in sorted((ROOT / "runs").glob("research_P*")):
    for cell in sorted((run / "cells").glob("*")):
        name = cell.name
        if "_S0_500000_" not in name:
            continue
        role = "B0" if "_B0_" in name else ("B2" if "_B2_" in name else "B1")
        if role == "B0" and not run.name.endswith("P1_SIZE_EP_VOL"):
            continue
        s = nav(cell)
        for code in ("563300.SH", "159907.SZ"):
            e = ETF[code]
            start = max(s.index[0], e.index[0])
            ss, ee = s[s.index >= start], e[(e.index >= start) & (e.index <= s.index[-1])]
            if len(ss) < 60:
                continue
            r = ee.pct_change().fillna(0)
            mix = (1 + 0.6 * r).cumprod()
            out.append({"run": run.name.replace("research_", ""), "cell": name.split("_A")[1][:1] if False else name, "role": role,
                        "etf": code, "from": str(start.date()), "strategy": stats(ss), "etf60": stats(mix), "etf100": stats(ee)})
(ROOT / "research_out/benchmark_compare.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
for o in out:
    st = o["cell"].split("REPLAY_DAILY_PROXY_")[-1] if "REPLAY" in o["cell"] else o["cell"]
    print(o["run"], o["role"], st[:22], o["etf"], o["from"], "策略 %+.1f%%/回撤%.0f%%" % (o["strategy"][0]*100, o["strategy"][1]*100),
          "60%%ETF %+.1f%%/%.0f%%" % (o["etf60"][0]*100, o["etf60"][1]*100), "100%%ETF %+.1f%%/%.0f%%" % (o["etf100"][0]*100, o["etf100"][1]*100))
