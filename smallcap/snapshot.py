"""只读事实快照适配器：把人工/脚本从 QMT、Tushare 只读导出的标准化事实（JSON）提供给同一决策引擎。

本模块不联网、不读凭据、不调用任何交易接口。快照中的事实可信度由其 purpose/来源记录决定，
TEST_ONLY 快照（由合成市场导出）不得作为真实数据使用。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .clock import TradingCalendar
from .contracts import ContractError, money, parse_date, parse_ts

DEC_FIELDS = ("open", "high", "low", "close", "amount", "ref_preclose", "mv")


class SnapshotMarket:
    def __init__(self, path_or_obj):
        d = path_or_obj if isinstance(path_or_obj, dict) else json.loads(Path(path_or_obj).read_text(encoding="utf-8"))
        if d.get("kind") != "SMALLCAP_SNAPSHOT":
            raise ContractError("SNAPSHOT_INVALID", "kind")
        self.purpose = d["purpose"]
        cal = d["calendar"]
        self.calendar = TradingCalendar([parse_date(x) for x in cal["sessions"]], parse_date(cal["known_through"]),
                                        cal.get("evidence", self.purpose))
        self.data_end = parse_date(d["data_end"])
        self.master = {}
        for sid, m in d["master"].items():
            m = dict(m)
            m["list_date"] = parse_date(m["list_date"])
            self.master[sid] = m
        self._bars = {}
        for sid, rows in d["bars"].items():
            for ds, b in rows.items():
                b = dict(b)
                for k in DEC_FIELDS:
                    if b.get(k) is not None:
                        b[k] = money(b[k])
                self._bars[(sid, parse_date(ds))] = b
        self.industries = d["industries"]
        self.risk_events = [dict(e, effective=parse_date(e["effective"]), available_at=parse_ts(e["available_at"]),
                                 end=parse_date(e["end"]) if e.get("end") else None) for e in d.get("risk_events", [])]
        self.factor_values = {parse_date(k): v for k, v in d.get("factor_values", {}).items()}
        self.financial_codes = frozenset(d.get("financial_codes", []))
        self.actions = [self._action(a) for a in d.get("actions", [])]
        self.lifecycle_events = [dict(e, date=parse_date(e["date"]), recovery_date=parse_date(e["recovery_date"]))
                                 for e in d.get("lifecycle_events", [])]

    @staticmethod
    def _action(a: dict) -> dict:
        a = dict(a)
        for k in ("record_date", "ex_date", "pay_date", "credit_date", "listing_date"):
            if a.get(k):
                a[k] = parse_date(a[k])
        return a

    def universe(self) -> list[str]:
        return sorted(self.master)

    def bar(self, sid: str, d: date) -> dict | None:
        return self._bars.get((sid, d))

    def industry(self, sid: str, d: date) -> str | None:
        return self.industries.get(sid)

    def risk_status(self, sid: str, d: date, asof: datetime) -> str:
        st = "NORMAL"
        for e in self.risk_events:
            if e["security_id"] == sid and e["available_at"] <= asof and e["effective"] <= d and \
                    (e["end"] is None or d < e["end"]):
                st = e["status"]
        return st

    def factor_value(self, sid: str, cutoff_day: date):
        v = self.factor_values.get(cutoff_day, {}).get(sid)
        return None if v is None else ("VALID", Decimal(v))

    def action_terms(self, a: dict) -> dict:
        return a

    def minute_bars(self, sid: str, d: date) -> list:
        return []


def export_snapshot(market, start: date, end: date, purpose: str = "TEST_ONLY") -> dict:
    """把一个市场对象（如 TEST_ONLY 合成市场）在 [start,end] 的事实导出为快照 JSON（仅工程联调）。"""
    sess = market.calendar.sessions_between(start, end)
    bars = {}
    for sid in market.universe():
        rows = {}
        for x in sess:
            b = market.bar(sid, x)
            if b is not None:
                rows[x.isoformat()] = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in b.items()}
        bars[sid] = rows
    fv = {}
    for x in market.calendar.week_ends(end):
        if start <= x <= end:
            vals = {}
            for sid in market.universe():
                v = market.factor_value(sid, x)
                vals[sid] = str(v[1]) if v and v[0] == "VALID" else None
            fv[x.isoformat()] = vals
    ser = lambda v: v.isoformat() if isinstance(v, (date, datetime)) else v
    return {"kind": "SMALLCAP_SNAPSHOT", "purpose": purpose,
            "calendar": {"sessions": [x.isoformat() for x in market.calendar.sessions],    # 日历可先于数据公布
                         "known_through": market.calendar.known_through.isoformat(), "evidence": purpose},
            "data_end": end.isoformat(),
            "master": {sid: {k: ser(v) for k, v in m.items() if k in ("security_id", "trade_code", "exchange", "board",
                                                                       "list_date", "security_type")}
                       for sid, m in market.master.items()},
            "bars": bars, "industries": {sid: market.industry(sid, end) for sid in market.universe()},
            "risk_events": [{k: ser(v) for k, v in e.items()} for e in getattr(market, "risk_events", [])],
            "factor_values": fv, "financial_codes": sorted(getattr(market, "financial_codes", [])),
            "actions": [{k: ser(v) for k, v in a.items()} for a in getattr(market, "actions", [])
                        if a.get("record_date") and start <= a["record_date"] <= end],
            "lifecycle_events": [{k: ser(v) for k, v in e.items()} for e in getattr(market, "lifecycle_events", [])]}
