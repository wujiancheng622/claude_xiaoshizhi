"""TEST_ONLY 合成市场：确定性伪随机生成的虚构证券（X0001.SH…），用于引擎联调与工程矩阵。

不是任何真实市场事实；证券代码、价格、公司行动、ST、退市均为虚构。不得用于投资结论。
价格以“分”为整数存储（tick=0.01），成交额按元，市值=收盘价×总股本。
"""
from __future__ import annotations

import math
import random
from array import array
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .clock import TradingCalendar
from .contracts import sh_dt

FIN_CODES = ("SW_BANK", "SW_NONBANK")
INDUSTRIES = tuple(f"SW{i:02d}" for i in range(1, 30)) + FIN_CODES


def synthetic_calendar(start: date, end: date) -> TradingCalendar:
    hol = set()
    for y in range(start.year, end.year + 1):
        hol |= {date(y, 1, 1), date(y, 5, 1), date(y, 10, 1), date(y, 10, 2), date(y, 10, 3)}
        # 春节附近一周（虚构）
        for k in range(5):
            hol.add(date(y, 2, 1) + timedelta(days=k + (y % 7)))
    days, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in hol:
            days.append(d)
        d += timedelta(days=1)
    return TradingCalendar(days, known_through=end, evidence="TEST_ONLY_SYNTHETIC")


class SyntheticMarket:
    """确定性合成数据。所有决策读取均须经引擎按 available_at 过滤。"""

    def __init__(self, seed: int = 7, n: int = 460, start: date = date(2009, 1, 5), end: date = date(2026, 10, 16),
                 data_end: date | None = None, with_events: bool = True):
        self.seed = seed
        self.calendar = synthetic_calendar(start, end + timedelta(days=10))
        self.sessions = [d for d in self.calendar.sessions if d <= end]
        self.data_end = data_end or self.sessions[-1]
        self.idx = {d: i for i, d in enumerate(self.sessions)}
        self.n = n
        self.with_events = with_events
        self.financial_codes = frozenset(FIN_CODES)       # 合成行业中的金融业代码（TEST_ONLY）
        rng = random.Random(seed)
        self.master: dict[str, dict] = {}
        for i in range(n):
            sid = f"X{i + 1:04d}.{'SH' if i % 2 == 0 else 'SZ'}"
            ex = "SH" if i % 2 == 0 else "SZ"
            lst = self.sessions[0] + timedelta(days=rng.randint(-2000, 2400))
            ind = INDUSTRIES[rng.randrange(len(INDUSTRIES) - 2)] if rng.random() > 0.05 else rng.choice(FIN_CODES)
            self.master[sid] = {"security_id": sid, "trade_code": sid, "exchange": ex,
                                "board": "MAIN_SH" if ex == "SH" else "MAIN_SZ", "list_date": lst,
                                "security_type": "A_ORDINARY", "industry": ind,
                                "shares": rng.randint(80, 900) * 1_000_000, "p0": rng.randint(250, 1600),
                                "vol": rng.uniform(0.018, 0.035), "turn": rng.uniform(0.006, 0.03),
                                "drift": rng.uniform(-0.0002, 0.0004)}
        self._bars: dict[str, dict] = {}
        self.actions: list[dict] = []
        self.risk_events: list[dict] = []
        self.lifecycle_events: list[dict] = []
        if with_events:
            self._make_events(rng)
        self._risk_by_sid: dict[str, list] = {}
        for e in self.risk_events:
            self._risk_by_sid.setdefault(e["security_id"], []).append(e)
        self._ex_by: dict[tuple, dict] = {(a["security_id"], a["ex_date"]): a for a in self.actions}

    # ---------------------------------------------------------------- 事件
    def _make_events(self, rng: random.Random) -> None:
        sids = sorted(self.master)
        for y in range(self.sessions[0].year, self.sessions[-1].year + 1):
            rd = self.calendar.session_on_or_after(date(y, 6, 15))
            if rd is None or rd > self.sessions[-1] or rd < self.sessions[0]:
                continue
            ex = self.calendar.next_session(rd)
            if ex > self.sessions[-1]:
                continue
            for sid in sids:
                r = rng.random()
                if r < 0.30:
                    self.actions.append({"action_id": f"DIV-{sid}-{y}", "security_id": sid, "kind": "CASH_DIVIDEND",
                                         "record_date": rd, "ex_date": ex, "pay_date": ex,
                                         "cash_per_share_bp": rng.randint(50, 200)})
                elif r < 0.33:
                    lst = self.calendar.next_session(ex)
                    self.actions.append({"action_id": f"BON-{sid}-{y}", "security_id": sid, "kind": "STOCK_DIVIDEND",
                                         "record_date": rd, "ex_date": ex, "pay_date": None, "credit_date": ex,
                                         "listing_date": lst, "shares_per_share": "0.3",
                                         "cash_in_lieu": "AT_EX_REFERENCE",
                                         "tax_treatment": "NOT_TAXABLE"})      # TEST_ONLY：按转增处理
        for sid in sids:
            r = rng.random()
            if r < 0.03:
                k = rng.randrange(300, len(self.sessions) - 30)
                eff = self.sessions[k]
                self.risk_events.append({"security_id": sid, "status": "ST", "effective": eff,
                                         "available_at": sh_dt(self.sessions[k - 1], time(18, 0)),
                                         "end": self.sessions[min(k + 250, len(self.sessions) - 1)]})
            elif r < 0.045:
                k = rng.randrange(300, len(self.sessions) - 100)
                arr = self.sessions[k]
                term = self.sessions[k + 15]
                self.risk_events.append({"security_id": sid, "status": "DELISTING_ARRANGEMENT", "effective": arr,
                                         "available_at": sh_dt(self.sessions[k - 1], time(18, 0)), "end": term})
                self.risk_events.append({"security_id": sid, "status": "TERMINATED", "effective": term,
                                         "available_at": sh_dt(self.sessions[k - 1], time(18, 0)), "end": None})
                self.lifecycle_events.append({"security_id": sid, "kind": "RESIDUAL", "date": term,
                                              "recovery_date": self.sessions[min(k + 75, len(self.sessions) - 1)],
                                              "recovery_per_share_cents": 30})

    def action_terms(self, a: dict) -> dict:
        self._series(a["security_id"])          # 现金分红额依赖除息前收盘，生成后才确定
        return a

    def risk_status(self, sid: str, d: date, asof: datetime) -> str:
        st = "NORMAL"
        for e in self._risk_by_sid.get(sid, ()):
            if e["available_at"] <= asof and e["effective"] <= d and (e["end"] is None or d < e["end"]):
                st = e["status"]
        return st

    def known_risk_events(self, asof: datetime) -> list[dict]:
        return [e for e in self.risk_events if e["available_at"] <= asof]

    # ---------------------------------------------------------------- 日线
    def _series(self, sid: str) -> dict:
        s = self._bars.get(sid)
        if s is not None:
            return s
        m = self.master[sid]
        rng = random.Random(f"{self.seed}/{sid}")
        n = len(self.sessions)
        ref, op, hi, lo, cl = (array("q", [0]) * n for _ in range(5))
        vol, st = array("q", [0]) * n, array("b", [0]) * n
        shares = array("q", [0]) * n
        price = m["p0"]
        sh = m["shares"]
        susp_left = 0
        term = next((e["effective"] for e in self._risk_by_sid.get(sid, []) if e["status"] == "TERMINATED"), None) \
            if hasattr(self, "_risk_by_sid") else None
        for i, d in enumerate(self.sessions):
            a = self._ex_by.get((sid, d)) if hasattr(self, "_ex_by") else None
            prev = price
            if a is not None:
                if a["kind"] == "CASH_DIVIDEND":
                    div = max(1, prev * a["cash_per_share_bp"] // 10000)
                    a["cash_per_share_cents"] = div
                    prev = prev - div
                else:
                    prev = int(Decimal(prev) / Decimal("1.3") + Decimal("0.5"))
                    sh = sh * 13 // 10
            ref[i] = prev
            shares[i] = sh
            listed = d >= m["list_date"] and (term is None or d < term)
            if not listed:
                st[i] = 2
                op[i] = hi[i] = lo[i] = cl[i] = prev
                price = prev
                continue
            if susp_left > 0 or rng.random() < 0.004:
                if susp_left == 0:
                    susp_left = rng.randint(1, 8)
                susp_left -= 1
                st[i] = 1
                op[i] = hi[i] = lo[i] = cl[i] = prev
                price = prev
                continue
            up, dn = (prev * 110 + 50) // 100, (prev * 90 + 50) // 100
            g = rng.gauss(0, m["vol"] * 0.3)
            o = min(max(int(prev * (1 + g)), dn), up)
            r = rng.gauss(m["drift"] - 0.003 * math.log(max(prev, 1) / m["p0"]), m["vol"])
            c = min(max(int(prev * (1 + r)), dn), up)
            c = max(c, 50)
            h = min(max(o, c) + abs(int(prev * rng.gauss(0, m["vol"] * 0.4))), up)
            lw = max(min(o, c) - abs(int(prev * rng.gauss(0, m["vol"] * 0.4))), dn)
            op[i], hi[i], lo[i], cl[i] = o, h, lw, c
            vol[i] = max(int(sh * m["turn"] * rng.uniform(0.4, 1.6)) // 100 * 100, 100)
            price = c
        s = {"ref": ref, "open": op, "high": hi, "low": lo, "close": cl, "vol": vol, "status": st, "shares": shares}
        self._bars[sid] = s
        return s

    def bar(self, sid: str, d: date) -> dict | None:
        i = self.idx.get(d)
        if i is None:
            return None
        s = self._series(sid)
        stv = s["status"][i]
        if stv == 2:
            return None
        cents = Decimal("0.01")
        o, h, l, c = (Decimal(s[k][i]) * cents for k in ("open", "high", "low", "close"))
        vol = s["vol"][i]
        amount = (Decimal(vol) * (o + c) / 2).quantize(Decimal("0.01"))
        ref = Decimal(s["ref"][i]) * cents
        return {"open": o, "high": h, "low": l, "close": c, "volume": vol, "amount": amount if stv == 0 else Decimal(0),
                "status": "TRADING" if stv == 0 else "SUSPENDED", "ref_preclose": ref,
                "mv": c * s["shares"][i]}

    def industry(self, sid: str, d: date) -> str | None:
        return self.master[sid]["industry"]

    def universe(self) -> list[str]:
        return sorted(self.master)

    def factor_value(self, sid: str, cutoff_day: date):
        """TEST_ONLY 夹具值：20日价格变化（不是投资因子）；约2%确定性缺失。"""
        i = self.idx[cutoff_day]
        if i < 20:
            return None
        rng = random.Random(f"{self.seed}/fv/{sid}/{cutoff_day}")
        if rng.random() < 0.02:
            return ("MISSING", None)
        s = self._series(sid)
        if s["status"][i] == 2 or s["close"][i - 20] <= 0:
            return None
        return ("VALID", Decimal(s["close"][i]) / Decimal(s["close"][i - 20]) - 1)

    # ---------------------------------------------------------------- 分钟（窗口内）
    def minute_bars(self, sid: str, d: date) -> list[dict]:
        """[t,t+1min) 区间，09:59 起至 10:29；仅在 bar_end 后可读。"""
        b = self.bar(sid, d)
        if b is None or b["status"] != "TRADING":
            return []
        rng = random.Random(f"{self.seed}/min/{sid}/{d}")
        out = []
        p = b["open"]
        tgt = (b["open"] + b["close"]) / 2
        per = max(b["volume"] // 240, 100)
        for k in range(-1, 30):
            start = datetime.combine(d, time(10, 0)) + timedelta(minutes=k)
            o = p
            drift = (tgt - b["open"]) / 31
            c = (o + drift + Decimal(str(round(rng.gauss(0, float(b["open"]) * 0.002), 2)))).quantize(Decimal("0.01"))
            c = min(max(c, b["low"]), b["high"])
            h = min(max(o, c) + Decimal("0.01") * rng.randint(0, 3), b["high"])
            lw = max(min(o, c) - Decimal("0.01") * rng.randint(0, 3), b["low"])
            v = int(per * rng.uniform(0.3, 1.7)) // 100 * 100
            out.append({"start": start, "open": o, "high": h, "low": lw, "close": c, "volume": v})
            p = c
        return out


_CACHE: dict = {}


def make_synthetic(seed: int, end_iso: str) -> "SyntheticMarket":
    """可跨进程序列化的工厂（functools.partial）；同一进程内复用只读合成市场。"""
    key = (seed, end_iso)
    if key not in _CACHE:
        _CACHE[key] = SyntheticMarket(seed=seed, start=date(2009, 1, 5), end=date.fromisoformat(end_iso))
    return _CACHE[key]
