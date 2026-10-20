"""核账：交易所昨收参考价与前一交易日收盘不同（发生除权除息）但分红送转表没有对应事件的情况（只读）。

主要来源：配股（V5 规定不认购，未认购损失是真实的）、漏记的分红送转。统计全市场与 V5D2 近似池内的发生数。
"""
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
ST = ROOT / "data" / "store_tushare_20261019"
meta = json.loads((ST / "meta.json").read_text())
dates, codes = meta["dates"], meta["codes"]
close = np.load(ST / "close.npy", mmap_mode="r")
pre = np.load(ST / "pre_close.npy", mmap_mode="r")
status = np.load(ST / "status.npy", mmap_mode="r")
acts = json.loads((ST / "actions.json").read_text())
have = {(a["security_id"], a["ex_date"]) for a in acts}
di = {d: i for i, d in enumerate(dates)}
both = (np.asarray(status[1:]) == 1) & (np.asarray(status[:-1]) == 1)
with np.errstate(invalid="ignore", divide="ignore"):
    gap = np.abs(np.asarray(pre[1:]) / np.asarray(close[:-1]) - 1)
cand = np.argwhere(both & (gap > 0.005))
unexpl = [(dates[i + 1], codes[j], float(gap[i, j])) for i, j in cand if (codes[j], dates[i + 1]) not in have]
print("adjustment days", len(cand), "explained", len(cand) - len(unexpl), "unexplained", len(unexpl))
# 池内（研究周度池，取所在周的池）
z = np.load(ROOT / "research_out" / "weekly_panel.npz", allow_pickle=True)
wd, pool, pc = [str(x) for x in z["dates"]], z["pool"], [str(x) for x in z["codes"]]
pci = {c: j for j, c in enumerate(pc)}
import bisect
inpool = 0
ex = []
for d, c, g in unexpl:
    k = bisect.bisect_right(wd, d) - 1
    j = pci.get(c)
    if k >= 0 and j is not None and pool[k, j]:
        inpool += 1
        ex.append((d, c, round(g, 4)))
print("unexplained while in the weekly pool:", inpool)
print(ex[:20])
(ROOT / "research_out" / "check_unexplained.json").write_text(json.dumps({"adjustment_days": len(cand), "unexplained": len(unexpl),
                                                                          "in_pool": inpool, "in_pool_examples": ex[:300]}, ensure_ascii=False))
