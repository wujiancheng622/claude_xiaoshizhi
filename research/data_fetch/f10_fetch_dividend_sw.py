"""只读抓取：分红送转（按除权除息日逐日）与申万2021一级行业成分（含历史纳入/剔除）。断点续抓，不写生产数据。

用法：<etf_data_fetcher venv python> f10_fetch_dividend_sw.py [dividend|sw|all]
"""
from __future__ import annotations

import sys
import time

import pandas as pd

from tsclient import PRIOR_RAW, RAW, factory

f = factory(delay=0.3)
DIV_FIELDS = ("ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,"
              "record_date,ex_date,pay_date,div_listdate,imp_ann_date,base_date,base_share")


def fetch_dividend():
    out = RAW / "dividend"
    out.mkdir(exist_ok=True)
    cal = pd.read_parquet(PRIOR_RAW / "trade_cal.parquet")
    days = sorted(cal.loc[(cal.is_open == 1) & (cal.cal_date >= "20100101") & (cal.cal_date <= "20261014"),
                          "cal_date"].astype(str))
    for ym in sorted({d[:6] for d in days}):
        p = out / f"{ym}.parquet"
        if p.exists():
            continue
        parts = []
        for d in [x for x in days if x[:6] == ym]:
            for attempt in range(5):
                try:
                    parts.append(f.call("dividend", ex_date=d, fields=DIV_FIELDS))
                    break
                except Exception as e:  # 代理偶发错误：退避重试；权限类错误不会走到这里（单次探测已确认有权限）
                    print("retry", d, type(e).__name__, str(e)[:120], flush=True)
                    time.sleep(10 * (attempt + 1))
            else:
                raise RuntimeError(f"dividend {d} failed")
        df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        df.to_parquet(p, index=False)
        print("dividend", ym, len(df), flush=True)


def fetch_sw():
    p = RAW / "sw2021_l1_members.parquet"
    if p.exists():
        return
    cls = f.call("index_classify", level="L1", src="SW2021")
    parts = []
    for code in cls.index_code:
        for is_new in ("Y", "N"):
            off = 0
            while True:
                df = f.call("index_member_all", l1_code=code, is_new=is_new, offset=off, limit=3000)
                parts.append(df)
                if len(df) < 3000:
                    break
                off += 3000
        print("sw", code, flush=True)
    df = pd.concat(parts, ignore_index=True).drop_duplicates()
    df.to_parquet(p, index=False)
    cls.to_parquet(RAW / "sw2021_l1_classify.parquet", index=False)
    print("sw members", len(df), flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("sw", "all"):
        fetch_sw()
    if what in ("dividend", "all"):
        fetch_dividend()
    print("DONE", what, flush=True)
