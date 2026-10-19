"""市场日历、周度截止、执行窗口与压力时钟（D03.1、D16、D18）。"""
from __future__ import annotations

import bisect
import calendar as _cal
from datetime import date, datetime, timedelta

from .contracts import (CUTOFF_TIME, PROXY_TIME, WINDOW_END, WINDOW_START, ContractError, sh_dt,
                        sh_local)


def add_months(d: date, n: int) -> date:
    """自然月加法，月底不足日取该月末日（2025-01-31 + 1月 = 2025-02-28）。"""
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    m += 1
    return date(y, m, min(d.day, _cal.monthrange(y, m)[1]))


class TradingCalendar:
    """经核验的交易日历。known_through 为日历权威覆盖到的自然日（含）。"""

    def __init__(self, sessions, known_through: date | None = None, evidence: str = "TEST_ONLY"):
        self.sessions: list[date] = sorted(set(sessions))
        if not self.sessions:
            raise ContractError("CALENDAR_EMPTY")
        self._index = {d: i for i, d in enumerate(self.sessions)}
        self.known_through = known_through or self.sessions[-1]
        self.evidence = evidence

    # -- 基础查询
    def is_session(self, d: date) -> bool:
        return d in self._index

    def index(self, d: date) -> int:
        if d not in self._index:
            raise ContractError("NOT_A_SESSION", d.isoformat())
        return self._index[d]

    def index_on_or_after(self, d: date) -> int:
        return bisect.bisect_left(self.sessions, d)

    def next_session(self, d: date, n: int = 1) -> date:
        """严格晚于 d 的第 n 个市场日。"""
        i = bisect.bisect_right(self.sessions, d) + n - 1
        if i >= len(self.sessions):
            raise ContractError("CALENDAR_EXHAUSTED", d.isoformat())
        return self.sessions[i]

    def prev_session(self, d: date, n: int = 1) -> date:
        i = bisect.bisect_left(self.sessions, d) - n
        if i < 0:
            raise ContractError("CALENDAR_EXHAUSTED", d.isoformat())
        return self.sessions[i]

    def session_on_or_after(self, d: date) -> date | None:
        i = bisect.bisect_left(self.sessions, d)
        return self.sessions[i] if i < len(self.sessions) else None

    def sessions_between(self, a: date, b: date) -> list[date]:
        """[a, b] 闭区间内的市场日。"""
        i, j = bisect.bisect_left(self.sessions, a), bisect.bisect_right(self.sessions, b)
        return self.sessions[i:j]

    def last_n_sessions(self, d: date, n: int, include: bool = True) -> list[date]:
        """截至 d（含或不含）的最近 n 个市场日；不足返回较短列表。"""
        j = bisect.bisect_right(self.sessions, d) if include else bisect.bisect_left(self.sessions, d)
        return self.sessions[max(0, j - n):j]

    # -- 周度截止
    def week_ends(self, data_end: date | None = None) -> list[date]:
        """完整自然周的最后市场日。周日须在日历权威范围内，且最后市场日不晚于数据终点。"""
        end = data_end or self.known_through
        out = []
        by_week: dict[tuple, date] = {}
        for d in self.sessions:
            key = d.isocalendar()[:2]
            by_week[key] = d  # sessions 升序，最后一次赋值即该周最后市场日
        for d in sorted(by_week.values()):
            sunday = d + timedelta(days=6 - d.weekday())
            if sunday <= self.known_through and d <= end:
                out.append(d)
        return out

    def first_week_end_in_year(self, year: int, data_end: date | None = None) -> date | None:
        """年内第一个完整自然周（周一至周日均在该年）的最后市场日；不取1月首个交易日。"""
        for d in self.week_ends(data_end):
            if d.year == year and (d - timedelta(days=d.weekday())).year == year:
                return d
        return None

    # -- 执行时钟
    def first_window_after(self, t: datetime) -> datetime:
        """D18.1：分钟窗口的首个允许行动时点。"""
        local = sh_local(t)
        d = local.date()
        if self.is_session(d):
            if local.time() < WINDOW_START:
                return sh_dt(d, WINDOW_START)
            if local.time() < WINDOW_END:
                # 下一个整分钟边界（含正好在边界上）
                minute = local.replace(second=0, microsecond=0)
                if minute != local:
                    minute += timedelta(minutes=1)
                if minute.time() < WINDOW_END:
                    return minute.astimezone(t.tzinfo)
        return sh_dt(self.next_session(d), WINDOW_START)

    def first_proxy_after(self, t: datetime) -> datetime:
        """日线代理：严格早于该日 09:30 可见的信息才能进入当日代理。"""
        local = sh_local(t)
        d = local.date()
        if self.is_session(d) and local.time() < PROXY_TIME:
            return sh_dt(d, PROXY_TIME)
        return sh_dt(self.next_session(d), PROXY_TIME)

    def cutoff(self, d: date) -> datetime:
        return sh_dt(d, CUTOFF_TIME)


def stress_block_days(cal: TradingCalendar, d0: date) -> tuple[date, date, date]:
    """S3：d0、d1、d2 三个市场日禁止作用范围卖出。"""
    return d0, cal.next_session(d0), cal.next_session(d0, 2)
