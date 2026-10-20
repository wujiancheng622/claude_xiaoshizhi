"""构建“研究数据仓”（临时 Tushare 来源，只读整理）：smallcap.research_market.ResearchMarket 的输入。

输入（只读）：
- ETF 仓库 2026-10-15 已抓：daily（按月）、daily_basic（2010-01..2011-04 日度）、daily_basic_mv（2011-05 起日度）、
  stock_basic、namechange、income；
- 本项目 2026-10-19 补抓：trade_cal_full、sw2021_l1_members、dividend（按除权除息日）。
输出：data/store_tushare_20261019/ 下 meta.json、*.npy（交易日×证券）、risk/industry/actions/lifecycle/statements.json。

口径（研究近似，均在 meta.json 写明）：
- 停牌：上市期内（上市日至最后交易日/退市日前）无日线记录视为确认停牌（Tushare 无记录=当日无成交）。
- 风险状态：名称含 ST → ST；名称以“退”结尾或含“退市” → DELISTING_ARRANGEMENT；自名称生效日起。
- 行业：申万2021一级，按纳入日期首尾相接；最早一条向前回填、历史断档沿用上一段（Tushare 申万历史有缺段，近似历史）。
- 公司行动：div_proc=实施；现金按 cash_div_tax（税前）；送转按 stk_div，送股部分（stk_bo_rate）按面值计税、转增不计税，
  拆分不明则计税口径未知；零碎股按中登“按尾数排序发放”规则，账户层保守按向下取整处理。
- 退市：到退市日仍持有的残余按 0 回收（保守口径）。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

PRIOR = Path("/home/wjc/src/github/etf/artifacts/stock_multifactor_20261015/raw")
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "store_tushare_20261019"
DATA_END = "20261014"


def board_of(code: str) -> str:
    sym, ex = code.split(".")
    if ex == "SH":
        return "STAR" if sym.startswith("688") else "MAIN_SH"
    return "CHINEXT" if sym.startswith(("300", "301")) else "MAIN_SZ"


def load_months(folder: Path, cols=None) -> pd.DataFrame:
    fs = sorted(glob.glob(str(folder / "*.parquet")))
    df = pd.concat([pd.read_parquet(f, columns=cols) for f in fs], ignore_index=True)
    df["ts_code"] = df.ts_code.astype(str)
    df["trade_date"] = df.trade_date.astype(str)
    return df[df.ts_code.str.endswith((".SH", ".SZ"))]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cal = pd.read_parquet(RAW / "trade_cal_full.parquet")
    sessions = sorted(str(x) for x in cal.loc[cal.is_open == 1, "cal_date"])
    known_through = str(cal.cal_date.max())

    d = load_months(PRIOR / "daily", ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"])
    d = d[d.trade_date <= DATA_END].drop_duplicates(["ts_code", "trade_date"])
    ok = (d.vol > 0) & (d.open > 0) & (d.close > 0) & (d.pre_close > 0)
    print("daily rows", len(d), "dropped non-trading/bad", int((~ok).sum()), flush=True)
    d = d[ok]
    dates = np.array(sorted(d.trade_date.unique()))
    sb = pd.read_parquet(PRIOR / "stock_basic.parquet")
    sb = sb[sb.ts_code.str.endswith((".SH", ".SZ"))].drop_duplicates("ts_code")
    codes = np.array(sorted(set(d.ts_code) | set(sb.ts_code)))
    di, ci = pd.Index(dates), pd.Index(codes)
    n, m = len(dates), len(codes)
    r, c = di.get_indexer(d.trade_date), ci.get_indexer(d.ts_code)
    for col, out in (("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"), ("pre_close", "pre_close")):
        a = np.full((n, m), np.nan)
        a[r, c] = d[col].to_numpy(float)
        if col == "close":
            close = a
        if col == "pre_close":
            pre = a
        np.save(OUT / f"{out}.npy", a)
    amt = np.full((n, m), np.nan)
    amt[r, c] = d.amount.to_numpy(float) * 1000.0          # 千元 → 元
    vol = np.full((n, m), np.nan)
    vol[r, c] = d.vol.to_numpy(float) * 100.0              # 手 → 股
    trading = np.zeros((n, m), dtype=bool)
    trading[r, c] = True
    del d

    # 上市区间与状态：1 交易，2 确认停牌，0 不在上市期
    sbi = sb.set_index("ts_code")
    lst = np.array([str(sbi.list_date.get(x)) if x in sbi.index and isinstance(sbi.list_date.get(x), str) else "" for x in codes])
    dl = np.array([str(sbi.delist_date.get(x)) if x in sbi.index and isinstance(sbi.delist_date.get(x), str) else "" for x in codes])
    first_trade = np.where(trading.any(0), trading.argmax(0), n)
    last_trade = np.where(trading.any(0), n - 1 - trading[::-1].argmax(0), -1)
    status = np.zeros((n, m), dtype=np.int8)
    for j in range(m):
        a = int(np.searchsorted(dates, lst[j])) if lst[j] else int(first_trade[j])
        a = min(a, int(first_trade[j]))
        if dl[j]:
            b = int(np.searchsorted(dates, dl[j]))           # 退市日（含）起不在上市期
        else:
            b = n
        if a < b:
            status[a:b, j] = 2
    status[trading] = 1
    np.save(OUT / "status.npy", status)
    # 停牌日：收盘/参考价沿用最近收盘；成交额 0
    close_ff = pd.DataFrame(close).ffill().to_numpy()
    np.save(OUT / "close.npy", np.where(status == 2, close_ff, close))
    np.save(OUT / "pre_close.npy", np.where(status == 2, close_ff, pre))
    np.save(OUT / "amount.npy", np.where(status == 2, 0.0, amt))
    np.save(OUT / "volume.npy", np.where(status == 2, 0.0, vol))
    del close, pre, amt, vol, close_ff

    # 市值与自由流通股本：2010-01..2011-04 daily_basic 日度，其后 daily_basic_mv
    b1 = load_months(PRIOR / "daily_basic", ["ts_code", "trade_date", "total_mv", "free_share"])
    b1 = b1[b1.trade_date < "20110501"]
    b2 = load_months(PRIOR / "daily_basic_mv", ["ts_code", "trade_date", "total_mv", "free_share"])
    b = pd.concat([b1, b2]).drop_duplicates(["ts_code", "trade_date"], keep="last")
    b = b[b.trade_date.isin(di) & b.ts_code.isin(ci)]
    r, c = di.get_indexer(b.trade_date), ci.get_indexer(b.ts_code)
    for col, name in (("total_mv", "mv_wan"), ("free_share", "free_share")):
        a = np.full((n, m), np.nan)
        a[r, c] = b[col].to_numpy(float)
        a = pd.DataFrame(a).ffill().to_numpy()
        a = np.where(status > 0, a, np.nan)
        if name == "free_share":
            a = a * 10000.0                                   # 万股 → 股
        np.save(OUT / f"{name}.npy", a)
    del b, b1, b2

    # 风险状态（名称区间）
    nc = pd.read_parquet(PRIOR / "namechange.parquet")
    nc = nc[nc.ts_code.str.endswith((".SH", ".SZ"))].dropna(subset=["start_date"]).copy()
    nc["start_date"] = nc.start_date.astype(str)
    nc["ann_date"] = nc.ann_date.fillna("").astype(str)
    nc = nc.sort_values(["ts_code", "start_date", "ann_date"]).drop_duplicates(["ts_code", "start_date"], keep="last")

    def status_of(name: str) -> str:
        if "ST" in name.upper():
            return "ST"
        if name.endswith("退") or "退市" in name:
            return "DELISTING_ARRANGEMENT"
        return "NORMAL"
    risk = {}
    for code, g in nc.groupby("ts_code"):
        rows = list(zip(g.start_date, g.name.astype(str)))
        iv = []
        for k, (s, name) in enumerate(rows):
            e = rows[k + 1][0] if k + 1 < len(rows) else None
            st = status_of(name)
            if st != "NORMAL":
                iv.append([s, e, st])
        if iv:
            risk[code] = iv
    (OUT / "risk.json").write_text(json.dumps(risk), encoding="utf-8")

    # 行业（申万2021一级）
    sw = pd.read_parquet(RAW / "sw2021_l1_members.parquet").sort_values(["ts_code", "in_date"])
    ind = {}
    for code, g in sw.groupby("ts_code"):
        rows = list(g.itertuples())
        iv = []
        for k, rr in enumerate(rows):
            s = "19900101" if k == 0 else str(rr.in_date)
            # 历史断档（Tushare 申万2021 历史缺段）：沿用上一段已知分类直到下一条记录（近似历史，研究口径）
            e = str(rows[k + 1].in_date) if k + 1 < len(rows) else None
            iv.append([s, e, rr.l1_code])
        ind[code] = iv
    (OUT / "industry.json").write_text(json.dumps(ind), encoding="utf-8")

    # 公司行动
    sess = np.array(sessions)

    def num(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if v == v else None

    def on_or_after(x):
        if not isinstance(x, str) or not x:
            return None
        k = int(np.searchsorted(sess, x))
        return sess[k] if k < len(sess) else None
    divf = sorted(glob.glob(str(RAW / "dividend" / "*.parquet")))
    actions, skipped = [], 0
    if divf:
        dv = pd.concat([pd.read_parquet(f) for f in divf], ignore_index=True)
        dv = dv[(dv.div_proc == "实施") & dv.ts_code.str.endswith((".SH", ".SZ")) & dv.ex_date.notna()]
        dv = dv.drop_duplicates(["ts_code", "ex_date", "end_date"])
        for x in dv.itertuples():
            rec, ex = on_or_after(x.record_date), on_or_after(str(x.ex_date))
            if rec is None or ex is None or ex > DATA_END:
                skipped += 1
                continue
            base = f"{x.ts_code}|{x.end_date}|{x.ex_date}"
            cash = num(x.cash_div_tax) or 0.0
            if cash > 0:
                actions.append({"action_id": base + "|C", "security_id": x.ts_code, "kind": "CASH_DIVIDEND",
                                "record_date": rec, "ex_date": ex, "pay_date": on_or_after(x.pay_date) or ex,
                                "cash_per_share_cents": repr(round(cash * 100, 6))})
            stk = num(x.stk_div) or 0.0
            if stk > 0:
                bo, co = num(x.stk_bo_rate), num(x.stk_co_rate)
                if bo and bo > 0:
                    treat, base_ps = "TAXABLE", repr(bo)            # 送红股按面值 1 元计税
                elif co and abs(co - stk) < 1e-9:
                    treat, base_ps = "NOT_TAXABLE", None             # 纯转增（股本溢价资本公积）
                else:
                    treat, base_ps = None, None                      # 送/转拆分不明：计税口径未知
                listing = on_or_after(x.div_listdate) or ex
                actions.append({"action_id": base + "|S", "security_id": x.ts_code, "kind": "STOCK_DIVIDEND",
                                "record_date": rec, "ex_date": ex, "credit_date": ex, "listing_date": listing,
                                "shares_per_share": repr(stk), "rounding": "FLOOR_ODD_LOT_LOTTERY",
                                "tax_treatment": treat, "tax_base_per_share": base_ps})
    # 核账：条款推算的除权参考价与交易所参考价（pre_close）不一致的事件打标（重整转增、同日送转漏记等）
    close_a = np.load(OUT / "close.npy", mmap_mode="r")
    pre_a = np.load(OUT / "pre_close.npy", mmap_mode="r")
    groups: dict = {}
    for a in actions:
        groups.setdefault((a["security_id"], a["ex_date"]), []).append(a)
    inconsistent = 0
    for (sid, ex), grp in groups.items():
        i, j = di.get_indexer([ex])[0], ci.get_indexer([sid])[0]
        if i <= 0 or j < 0 or status[i, j] != 1 or status[i - 1, j] != 1:
            continue
        cash_ps = sum(float(a["cash_per_share_cents"]) for a in grp if a["kind"] == "CASH_DIVIDEND") / 100.0
        ratio = sum(float(a["shares_per_share"]) for a in grp if a["kind"] == "STOCK_DIVIDEND")
        ref = round((float(close_a[i - 1, j]) - cash_ps) / (1 + ratio) + 1e-9, 2)
        got = float(pre_a[i, j])
        # 只标实质性不一致（>5%：重整转增、漏记送转等）；小差异多为取整或条款细节，不作阻断
        if abs(ref - got) > max(0.011, 0.05 * got):
            inconsistent += 1
            for a in grp:
                a["terms_status"] = "INCONSISTENT_WITH_EXCHANGE_REFERENCE"
                a["exchange_reference"] = repr(got)
                a["implied_reference"] = repr(ref)
    (OUT / "actions.json").write_text(json.dumps(actions), encoding="utf-8")
    print("action groups inconsistent with exchange reference:", inconsistent, flush=True)

    # 退市：零回收
    life = []
    for j, code in enumerate(codes):
        if dl[j] and dl[j] <= DATA_END:
            dd = on_or_after(dl[j])
            if dd and dd <= DATA_END:
                life.append({"security_id": code, "date": dd, "recovery_date": dd, "recovery_per_share_cents": 0,
                             "evidence": "TUSHARE_STOCK_BASIC_DELIST_DATE_ZERO_RECOVERY"})
    (OUT / "lifecycle.json").write_text(json.dumps(life), encoding="utf-8")

    # 财报：归母净利润累计值，合并报表，同一报告期首次披露版本（f_ann_date 优先）
    inc = pd.read_parquet(PRIOR / "income.parquet")
    inc = inc[(inc.report_type.astype(str) == "1") & inc.ts_code.str.endswith((".SH", ".SZ"))].copy()
    inc["ann"] = inc.f_ann_date.fillna(inc.ann_date)
    inc = inc.dropna(subset=["ann", "n_income_attr_p"])
    inc["ann"] = inc.ann.astype(str)
    inc["_uf"] = inc.update_flag.astype(str)
    inc = inc.sort_values(["ts_code", "end_date", "ann", "_uf"], kind="mergesort").drop_duplicates(
        ["ts_code", "end_date"], keep="first")
    inc = inc[inc.end_date.str[4:].isin(["0331", "0630", "0930", "1231"])]
    stm = {code: [[e, a, repr(float(v))] for e, a, v in zip(g.end_date, g.ann, g.n_income_attr_p)]
           for code, g in inc.groupby("ts_code")}
    (OUT / "statements.json").write_text(json.dumps(stm), encoding="utf-8")

    meta = {"purpose": "TUSHARE_TEMP_RESEARCH", "built_from": ["ETF artifacts stock_multifactor_20261015/raw",
                                                                "data/raw (2026-10-19)"],
            "sessions": sessions, "known_through": known_through, "calendar_evidence": "TUSHARE_TRADE_CAL_SSE",
            "data_end": DATA_END, "dates": dates.tolist(), "codes": codes.tolist(),
            "exchange": [x.split(".")[1] for x in codes], "board": [board_of(x) for x in codes],
            "list_date": lst.tolist(), "delist_date": dl.tolist(), "financial_codes": ["801780.SI", "801790.SI"],
            "assumptions": __doc__, "actions": len(actions), "actions_skipped": skipped, "lifecycle": len(life),
            "action_groups_inconsistent": inconsistent}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print("store", OUT, n, m, "actions", len(actions), "skipped", skipped, "delisted", len(life), flush=True)


if __name__ == "__main__":
    main()
