"""核账：用分红送转条款推算的除权参考价，与交易所昨收参考价（Tushare pre_close）逐条比对（只读）。

参考价=(除权前一交易日收盘−每股税前派现)÷(1+每股送转)，按交易所规则四舍五入到分；
比对容差 0.01 元（取整差）。不一致的事件列出供核查，不自动改数据。
"""
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
ST = ROOT / "data" / "store_tushare_20261019"
meta = json.loads((ST / "meta.json").read_text())
dates = meta["dates"]
di = {d: i for i, d in enumerate(dates)}
ci = {c: j for j, c in enumerate(meta["codes"])}
close = np.load(ST / "close.npy", mmap_mode="r")
pre = np.load(ST / "pre_close.npy", mmap_mode="r")
status = np.load(ST / "status.npy", mmap_mode="r")
acts = json.loads((ST / "actions.json").read_text())
by = {}
for a in acts:
    by.setdefault((a["security_id"], a["ex_date"]), []).append(a)
ok = bad = skip = 0
worst = []
for (sid, ex), group in by.items():
    i, j = di.get(ex), ci.get(sid)
    if i is None or j is None or i == 0 or status[i, j] != 1 or status[i - 1, j] != 1:
        skip += 1
        continue
    cash = sum((Decimal(a.get("cash_per_share_cents", "0")) for a in group if a["kind"] == "CASH_DIVIDEND"), Decimal(0)) / 100
    ratio = sum((Decimal(a["shares_per_share"]) for a in group if a["kind"] == "STOCK_DIVIDEND"), Decimal(0))
    last = Decimal(repr(float(close[i - 1, j])))
    ref = ((last - cash) / (1 + ratio)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    got = Decimal(repr(float(pre[i, j])))
    if abs(ref - got) <= Decimal("0.01"):
        ok += 1
    else:
        bad += 1
        worst.append((float(abs(ref - got) / got), sid, ex, str(last), str(cash), str(ratio), str(ref), str(got)))
worst.sort(reverse=True)
print(f"events {len(by)}  checked {ok + bad}  match {ok}  mismatch {bad}  ({bad / max(ok + bad, 1):.2%})  skipped {skip}")
for w in worst[:15]:
    print(w)
(ROOT / "research_out" / "check_actions.json").write_text(json.dumps({"match": ok, "mismatch": bad, "skipped": skip,
                                                                      "worst": worst[:200]}, ensure_ascii=False, indent=1))
