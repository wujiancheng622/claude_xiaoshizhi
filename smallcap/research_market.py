"""研究用只读行情适配器：从 research/data_prep/build_store.py 生成的“研究数据仓”读取（numpy 支撑，可内存映射）。

来源：临时 Tushare 研究快照（QMT 维护期）。按 V5D2 只作研究回放，不认证为 QMT 主源；
每个事实保留来源标签，适配器只做格式转换（单位、Decimal、as-of），不做投资判断。

与引擎的接口同 SyntheticMarket/SnapshotMarket：calendar, data_end, master, universe(), bar(), industry(),
risk_status(), actions, action_terms(), lifecycle_events, minute_bars(), financial_codes；
另提供 mv_order()（池构建按市值顺序惰性评估）与 factor_inputs()（因子原始输入，含 available_at）。
"""
from __future__ import annotations

import bisect
import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from .clock import TradingCalendar
from .contracts import ContractError, sh_dt
from .factors import SOURCE_UNKNOWN, STALE, VALID

STATUS = {1: "TRADING", 2: "SUSPENDED"}


def _d(x: str) -> date:
    return date(int(x[:4]), int(x[4:6]), int(x[6:8]))


def _dec(x) -> Decimal | None:
    x = float(x)
    return None if x != x else Decimal(repr(x))


class ResearchMarket:
    def __init__(self, store_dir: str | Path):
        import numpy as np                      # 研究适配器的可选依赖；生产核心不依赖 numpy
        self.np = np
        self.dir = Path(store_dir)
        meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        self.purpose = meta["purpose"]
        self.cache_token = ("RESEARCH_STORE", str(self.dir.resolve()), meta.get("data_end"), len(meta["codes"]))
        self.calendar = TradingCalendar([_d(x) for x in meta["sessions"]], _d(meta["known_through"]),
                                        meta.get("calendar_evidence", self.purpose))
        self.data_end = _d(meta["data_end"])
        self.dates = [_d(x) for x in meta["dates"]]
        self.date_idx = {d: i for i, d in enumerate(self.dates)}
        self.codes = list(meta["codes"])
        self.code_idx = {c: j for j, c in enumerate(self.codes)}
        self.financial_codes = frozenset(meta["financial_codes"])
        self.master = {}
        for c, ex, bd, lst, dl in zip(self.codes, meta["exchange"], meta["board"], meta["list_date"], meta["delist_date"]):
            self.master[c] = {"security_id": c, "trade_code": c, "exchange": ex, "board": bd,
                              "list_date": _d(lst) if lst else date(9999, 12, 31), "delist_date": _d(dl) if dl else None,
                              "security_type": "A_ORDINARY"}
        arr = {}
        for name in ("open", "high", "low", "close", "pre_close", "amount", "volume", "mv_wan", "free_share", "status"):
            arr[name] = np.load(self.dir / f"{name}.npy", mmap_mode="r")
        self.a = arr
        self._risk = {c: [(_d(s), _d(e) if e else None, st) for s, e, st in v]
                      for c, v in json.loads((self.dir / "risk.json").read_text(encoding="utf-8")).items()}
        self._ind = {c: [(_d(s), _d(e) if e else None, code) for s, e, code in v]
                     for c, v in json.loads((self.dir / "industry.json").read_text(encoding="utf-8")).items()}
        self.actions = []
        for a in json.loads((self.dir / "actions.json").read_text(encoding="utf-8")):
            for k in ("record_date", "ex_date", "pay_date", "credit_date", "listing_date"):
                if a.get(k):
                    a[k] = _d(a[k])
            self.actions.append(a)
        self.lifecycle_events = [dict(e, date=_d(e["date"]), recovery_date=_d(e["recovery_date"]))
                                 for e in json.loads((self.dir / "lifecycle.json").read_text(encoding="utf-8"))]
        # 财报：只有公告日期 → 自其后首个市场日 09:30 起可用（data.announcement_available_at 同一口径）
        self._stmt = {}
        for c, rows in json.loads((self.dir / "statements.json").read_text(encoding="utf-8")).items():
            recs = []
            for p, a, v in rows:
                try:
                    avail = sh_dt(self.calendar.next_session(_d(a)), time(9, 30))
                except ContractError:
                    continue                         # 日历之后才公告：在研究区间内不可见
                recs.append((avail, _d(p), Decimal(v)))
            recs.sort(key=lambda r: (r[0], r[1]))
            self._stmt[c] = recs
        self._mv_order_cache: dict = {}

    # ------------------------------------------------------------ 基本事实
    def universe(self) -> list[str]:
        return self.codes

    def _ij(self, sid: str, d: date):
        i = self.date_idx.get(d)
        j = self.code_idx.get(sid)
        return i, j

    def bar(self, sid: str, d: date) -> dict | None:
        i, j = self._ij(sid, d)
        if i is None or j is None:
            return None
        a = self.a
        st = int(a["status"][i, j])
        close = _dec(a["close"][i, j])
        if st == 0 or close is None:
            return None                      # 不在上市期，或停牌但此前从未成交（无价格事实）
        mv = _dec(a["mv_wan"][i, j])
        b = {"status": STATUS[st], "close": close, "ref_preclose": _dec(a["pre_close"][i, j]),
             "mv": mv * 10000 if mv is not None else None}
        if st == 1:
            b.update(open=_dec(a["open"][i, j]), high=_dec(a["high"][i, j]), low=_dec(a["low"][i, j]),
                     amount=_dec(a["amount"][i, j]), volume=int(round(float(a["volume"][i, j]))))
        else:
            b.update(open=None, high=None, low=None, amount=Decimal(0), volume=0)
        return b

    def amount_history(self, sid: str, d: date, n: int) -> list:
        """含 d 的最近 n 个市场日 (日期, 成交额元|None, TRADING/SUSPENDED/MISSING)；上市期外为 MISSING。"""
        i, j = self._ij(sid, d)
        out = []
        for k in range(i - n + 1, i + 1):
            if k < 0:
                continue
            st = int(self.a["status"][k, j])
            if st == 1 and self.a["amount"][k, j] == self.a["amount"][k, j]:
                out.append((self.dates[k], _dec(self.a["amount"][k, j]), "TRADING"))
            elif st == 2 and self.a["close"][k, j] == self.a["close"][k, j]:
                out.append((self.dates[k], Decimal(0), "SUSPENDED"))
            else:
                out.append((self.dates[k], None, "MISSING"))
        return out

    def industry(self, sid: str, d: date) -> str | None:
        for s, e, code in self._ind.get(sid, ()):
            if s <= d and (e is None or d < e):
                return code
        return None

    def risk_status(self, sid: str, d: date, asof: datetime) -> str:
        st = "NORMAL"
        for s, e, status in self._risk.get(sid, ()):
            if s <= d and (e is None or d < e) and sh_dt(s, time(0, 0)) <= asof:
                st = status
        return st

    def action_terms(self, a: dict) -> dict:
        return a

    def minute_bars(self, sid: str, d: date) -> list:
        return []

    def factor_value(self, sid: str, d: date):
        return None

    # ------------------------------------------------------------ 池构建：按 (市值, 代码) 顺序
    def mv_order(self, d: date) -> tuple[list[str], list[str]]:
        """返回 (已知市值按升序的上市证券, 上市但市值未知的证券)。"""
        if d in self._mv_order_cache:
            return self._mv_order_cache[d]
        np = self.np
        i = self.date_idx[d]
        st = np.asarray(self.a["status"][i])
        mv = np.asarray(self.a["mv_wan"][i])
        listed = st > 0
        known = listed & np.isfinite(mv)
        idx = np.flatnonzero(known)
        codes = np.array(self.codes, dtype=object)
        order = idx[np.lexsort((codes[idx], mv[idx]))]
        res = ([self.codes[j] for j in order], [self.codes[j] for j in np.flatnonzero(listed & ~known)])
        self._mv_order_cache = {d: res}             # 只保留最近一个日期
        return res

    # ------------------------------------------------------------ 因子原始输入（as-of）
    def _np_ttm(self, sid: str, cutoff: datetime):
        rows = self._stmt.get(sid)
        if not rows:
            return (None, SOURCE_UNKNOWN, None)
        have = {}
        for avail, period, val in rows:
            if avail > cutoff:
                break
            have[period] = (val, avail)
        if not have:
            return (None, SOURCE_UNKNOWN, None)
        last = max(have)
        if cutoff.date() > _add_months(last, 15):
            return (None, STALE, have[last][1])
        if last.month == 12:
            return (have[last][0], VALID, have[last][1])
        fy, same = date(last.year - 1, 12, 31), date(last.year - 1, last.month, last.day)
        if fy not in have or same not in have:
            return (None, SOURCE_UNKNOWN, have[last][1])
        val = have[last][0] + have[fy][0] - have[same][0]
        return (val, VALID, max(have[last][1], have[fy][1], have[same][1]))

    def _windows(self, sid: str, d: date):
        i = self.date_idx[d]
        j = self.code_idx[sid]
        lo = i - 59
        if lo < 0:
            return None, None
        a = self.a
        st = a["status"][lo:i + 1, j]
        ret, turn = [], []
        for k in range(60):
            s = int(st[k])
            if s != 1:
                ret.append(None)
                turn.append(None)
                continue
            ii = lo + k
            ret.append((_dec(a["close"][ii, j]), _dec(a["pre_close"][ii, j])))
            fs = a["free_share"][ii, j]
            turn.append((_dec(a["volume"][ii, j]), _dec(fs) * 10000) if fs == fs else None)
        return ret, turn

    def factor_inputs(self, sid: str, d: date, cutoff: datetime, fields: set) -> dict:
        out = {}
        close_t = sh_dt(d, time(15, 0))
        if "market_value_total" in fields:
            b = self.bar(sid, d)
            mv = b["mv"] if b else None
            out["market_value_total"] = (mv, VALID if mv is not None else SOURCE_UNKNOWN, close_t)
        if "np_parent_ttm" in fields:
            out["np_parent_ttm"] = self._np_ttm(sid, cutoff)
        if fields & {"daily_return_window_60", "turnover_window_60"}:
            ret, turn = self._windows(sid, d)
            if "daily_return_window_60" in fields:
                out["daily_return_window_60"] = (ret, VALID if ret is not None else SOURCE_UNKNOWN, close_t)
            if "turnover_window_60" in fields:
                out["turnover_window_60"] = (turn, VALID if turn is not None else SOURCE_UNKNOWN, close_t)
        return out


def _add_months(d: date, n: int) -> date:
    from .clock import add_months
    return add_months(d, n)
