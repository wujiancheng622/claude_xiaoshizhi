import random
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from smallcap import contracts as C
from smallcap.clock import TradingCalendar
from smallcap.contracts import ContractError, sh_dt
from smallcap.data import (DecisionUsage, ObservedValue, StatementRecord, announcement_available_at,
                           financial_fresh, impact_scope, latest_visible_period, merge_basis_series,
                           merge_dividend_rows, normalize, ratio_validity, realtime_quote_usable, select_source,
                           single_quarter, statement_asof, ttm, ttm_crosscheck, tushare_mapping)
from tests.acceptance import case
from tests.fixtures import EventFactory, buy_order, fill, new_ledger, weekday_calendar

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ov(src, value, rid=None, quantum=None, quality="OK", hist=True, basis="RAW", unit="CNY", avail=T0):
    return ObservedValue(rid or f"{src}-1", "X0001.SH", "raw_close", "2025-06-30", src,
                         None if value is None else Decimal(value), unit, Decimal(quantum) if quantum else None, basis,
                         avail, quality=quality, historical_semantics=hist)


class G02(unittest.TestCase):
    @case("G02-01")
    def test_qmt_primary_when_equal(self):
        r = select_source("raw_close", ov("QMT", "10.00", "q1"), ov("TUSHARE", "10.00", "t1"))
        self.assertTrue(r.ok)
        self.assertEqual((r.value.source, r.value.value, r.value.action), ("QMT", Decimal("10.00"), "PRIMARY"))
        self.assertEqual(r.value.trail, ("q1", "t1"))

    @case("G02-02")
    def test_historical_semantics_substitute(self):
        r = select_source("market_value_total", ov("QMT", "100", hist=False), ov("TUSHARE", "120", "t9"))
        self.assertEqual((r.value.source, r.value.action, r.value.reason_code),
                         ("TUSHARE", "SUBSTITUTE", "HISTORICAL_SEMANTICS_UNAVAILABLE"))
        self.assertEqual(r.value.value, Decimal(120))

    @case("G02-03")
    def test_qmt_missing_explicit_substitute(self):
        r = select_source("raw_close", None, ov("TUSHARE", "9.50", "t2"))
        self.assertEqual((r.value.source, r.value.action), ("TUSHARE", "SUBSTITUTE"))
        self.assertIn("SUBSTITUTE", r.codes())

    @case("G02-04")
    def test_daily_value_not_realtime_quote(self):
        now = datetime(2026, 1, 5, 2, 0, 5, tzinfo=timezone.utc)
        self.assertFalse(realtime_quote_usable(now - timedelta(seconds=30), now, kind="TICK"))   # QMT过期报价
        self.assertFalse(realtime_quote_usable(now, now, kind="DAILY_HISTORY"))                  # TS日值
        from smallcap.execution import quote_fresh
        self.assertFalse(quote_fresh(now, now, mode="DAILY"))

    @case("G02-05")
    def test_wrong_qmt_value_rejected(self):
        r = select_source("raw_close", ov("QMT", "-1.00"), ov("TUSHARE", "10.00", "t3"), validity=lambda v: v > 0)
        self.assertEqual((r.value.source, r.value.value, r.value.reason_code), ("TUSHARE", Decimal("10.00"), "PROVEN_ERROR"))

    @case("G02-06")
    def test_conflict(self):
        r = select_source("raw_close", ov("QMT", "10"), ov("TUSHARE", "11"))
        self.assertEqual(r.status, C.UNKNOWN)
        self.assertIsNone(r.value.value)
        self.assertEqual(r.codes(), ["SOURCE_CONFLICT"])

    @case("G02-07")
    def test_rounding_explained(self):
        r = select_source("raw_close", ov("QMT", "10.00", quantum="0.01"), ov("TUSHARE", "10.004", quantum="0.001"))
        self.assertEqual((r.value.value, r.value.action), (Decimal("10.00"), "ROUNDING_EXPLAINED"))
        r2 = select_source("raw_close", ov("QMT", "10.00", quantum="0.01"), ov("TUSHARE", "10.006", quantum="0.001"))
        self.assertEqual(r2.status, C.UNKNOWN)

    def _norm(self, endpoint, field, v):
        return normalize({"record_id": "r", "security_id": "X0001.SH", "period": "2025-06-30", field: v},
                         tushare_mapping(endpoint, field))

    @case("G02-08")
    def test_amount_unit(self):
        self.assertEqual(self._norm("daily", "amount", "10000").value, Decimal(10_000_000))

    @case("G02-09")
    def test_volume_unit(self):
        self.assertEqual(self._norm("daily", "vol", "35").value, Decimal(3500))

    @case("G02-10")
    def test_total_mv_unit(self):
        self.assertEqual(self._norm("daily_basic", "total_mv", "120000").value, Decimal(1_200_000_000))

    @case("G02-11")
    def test_basis_mismatch(self):
        self.assertEqual(merge_basis_series("QFQ@2024-06-30", "QFQ@2025-06-30").codes(), ["BASIS_MISMATCH"])
        self.assertTrue(merge_basis_series("RAW", "RAW").ok)

    @case("G02-12")
    def test_ts_cannot_fill_account(self):
        r = select_source("broker_cash", None, ov("TUSHARE", "1000000"))
        self.assertEqual(r.status, C.UNKNOWN)
        self.assertEqual(r.codes(), ["ACCOUNT_FACT_UNAVAILABLE"])

    @case("G02-13")
    def test_impact_replays_from_earliest(self):
        usages = [DecisionUsage(f"plan-{y}", datetime(y, 6, 4, 12, tzinfo=timezone.utc),
                                frozenset({("score", "X0001.SH", f"{y}")})) for y in (2020, 2021, 2022)]
        usages.append(DecisionUsage("plan-2021b", datetime(2021, 9, 3, 12, tzinfo=timezone.utc),
                                    frozenset({("score", "X0001.SH", "2021"), ("x", "y", "z")})))
        r = impact_scope([("score", "X0001.SH", "2021")], usages)
        self.assertEqual(r.value["replay_from"], "plan-2021")
        self.assertEqual(r.value["superseded"], ["plan-2021", "plan-2022", "plan-2021b"])

    @case("G02-14")
    def test_unused_field_no_impact(self):
        usages = [DecisionUsage("p", T0, frozenset({("score", "X", "1")}))]
        r = impact_scope([("unused_field", "X", "1")], usages)
        self.assertEqual(r.value["impact"], "NO_IMPACT")
        self.assertIsNone(r.value["replay_from"])

    @case("G02-15")
    def test_row_shuffle_invariant(self):
        recs = [StatementRecord("X", "IS", date(2025, 6, 30), "CONS", "np", Decimal(i), T0 + timedelta(days=i), i,
                                i - 1 if i else None) for i in range(5)]
        key = ("X", "IS", date(2025, 6, 30), "CONS", "np")
        base = statement_asof(recs, key, T0 + timedelta(days=10)).value.value
        rng = random.Random(1)
        for _ in range(20):
            rr = recs[:]
            rng.shuffle(rr)
            self.assertEqual(statement_asof(rr, key, T0 + timedelta(days=10)).value.value, base)
        a = select_source("raw_close", ov("QMT", "10"), ov("TUSHARE", "10"))
        b = select_source("raw_close", ov("QMT", "10"), ov("TUSHARE", "10"))
        self.assertEqual(a.value, b.value)

    @case("G02-16")
    def test_dividend_total_and_parts_not_summed(self):
        rows = [{"event_id": "E1", "component": "TOTAL", "cash_div_tax": "1.4"},
                {"event_id": "E1", "component": "PART", "part_id": "a", "cash_div_tax": "1.0"},
                {"event_id": "E1", "component": "PART", "part_id": "b", "cash_div_tax": "0.4"}]
        r = merge_dividend_rows(rows)
        self.assertEqual(r.value["E1"], Decimal("1.4"))
        bad = rows + [{"event_id": "E1", "component": "PART", "part_id": "c", "cash_div_tax": "0.1"}]
        self.assertEqual(merge_dividend_rows(bad).status, C.UNKNOWN)


class G03(unittest.TestCase):
    cal = weekday_calendar(date(2024, 1, 1), date(2027, 12, 31), holidays=[date(2026, 4, 3)])

    @case("G03-01")
    def test_report_not_visible_before_announcement(self):
        recs = [StatementRecord("X", "IS", date(2025, 12, 31), "CONS", "np", Decimal(5),
                                announcement_available_at(self.cal, date(2026, 4, 17)))]
        r = statement_asof(recs, ("X", "IS", date(2025, 12, 31), "CONS", "np"), sh_dt(date(2025, 12, 31), time(20)))
        self.assertEqual(r.codes(), ["NOT_YET_VISIBLE"])

    @case("G03-02")
    def test_date_only_next_session_0930(self):
        fri = date(2026, 1, 9)
        a = announcement_available_at(self.cal, fri)
        self.assertEqual(a, sh_dt(date(2026, 1, 12), time(9, 30)))
        self.assertGreater(a, sh_dt(fri, time(20)))

    @case("G03-03")
    def test_trusted_timestamp(self):
        fri = date(2026, 1, 9)
        cutoff = sh_dt(fri, time(20))
        self.assertLessEqual(announcement_available_at(self.cal, fri, sh_dt(fri, time(19))), cutoff)
        self.assertGreater(announcement_available_at(self.cal, fri, sh_dt(fri, time(20, 1))), cutoff)

    @case("G03-04")
    def test_ttm_available_at_is_max(self):
        q1 = (Decimal(30), sh_dt(date(2026, 4, 20), time(18)))
        full = (Decimal(120), sh_dt(date(2026, 4, 28), time(18)))
        same = (Decimal(20), sh_dt(date(2025, 4, 25), time(18)))
        r = ttm(q1, full, same, 1)
        self.assertEqual(r.value[1], full[1])

    @case("G03-05")
    def test_ttm_value(self):
        t = (Decimal(0), T0)
        r = ttm((Decimal(30), T0), (Decimal(120), T0), (Decimal(20), T0), 1)
        self.assertEqual(r.value[0], Decimal(130))
        self.assertTrue(ttm_crosscheck(Decimal(130), [Decimal(30), Decimal(35), Decimal(30), Decimal(35)]))
        self.assertFalse(ttm_crosscheck(Decimal(130), [Decimal(30), Decimal(35), Decimal(30), Decimal(36)]))

    @case("G03-06")
    def test_single_quarter(self):
        self.assertEqual(single_quarter({1: (Decimal(30), T0), 2: (Decimal(80), T0)}, 2).value[0], Decimal(50))
        self.assertEqual(single_quarter({2: (Decimal(80), T0)}, 2).status, C.UNKNOWN)

    @case("G03-07")
    def test_revision_visibility(self):
        k = ("X", "IS", date(2025, 12, 31), "CONS", "np")
        recs = [StatementRecord(*k, Decimal(10), sh_dt(date(2026, 3, 30), time(18)), 0),
                StatementRecord(*k, Decimal(12), sh_dt(date(2026, 5, 1), time(18)), 1, 0)]
        self.assertEqual(statement_asof(recs, k, sh_dt(date(2026, 4, 30), time(20))).value.value, Decimal(10))
        self.assertEqual(statement_asof(recs, k, sh_dt(date(2026, 5, 6), time(20))).value.value, Decimal(12))

    @case("G03-08")
    def test_same_time_conflict(self):
        k = ("X", "IS", date(2025, 12, 31), "CONS", "np")
        t = sh_dt(date(2026, 3, 30), time(18))
        recs = [StatementRecord(*k, Decimal(10), t, None), StatementRecord(*k, Decimal(11), t, None)]
        for order in (recs, recs[::-1]):
            self.assertEqual(statement_asof(order, k, t + timedelta(days=1)).codes(), ["SOURCE_CONFLICT"])

    @case("G03-09")
    def test_future_append_prefix_invariant(self):
        k = ("X", "IS", date(2024, 12, 31), "CONS", "np")
        recs = [StatementRecord(*k, Decimal(10), sh_dt(date(2025, 3, 30), time(18)), 0)]
        cutoff = sh_dt(date(2025, 6, 6), time(20))
        before = (statement_asof(recs, k, cutoff).value, latest_visible_period(recs, "X", "IS", "CONS", "np", cutoff))
        recs2 = recs + [StatementRecord("X", "IS", date(2027, 6, 30), "CONS", "np", Decimal(99),
                                        sh_dt(date(2027, 8, 30), time(18)), 0)]
        after = (statement_asof(recs2, k, cutoff).value, latest_visible_period(recs2, "X", "IS", "CONS", "np", cutoff))
        self.assertEqual(before, after)

    @case("G03-10")
    def test_latest_invalid_not_fallback(self):
        t = sh_dt(date(2026, 4, 1), time(18))
        recs = [StatementRecord("X", "BS", date(2024, 12, 31), "CONS", "equity", Decimal(100), t - timedelta(days=365)),
                StatementRecord("X", "BS", date(2025, 12, 31), "CONS", "equity", Decimal(-5), t)]
        cutoff = t + timedelta(days=3)
        lp = latest_visible_period(recs, "X", "BS", "CONS", "equity", cutoff)
        self.assertEqual(lp, date(2025, 12, 31))
        v = statement_asof(recs, ("X", "BS", lp, "CONS", "equity"), cutoff).value.value
        self.assertEqual(ratio_validity(Decimal(1), v), "MODEL_INAPPLICABLE")
        self.assertEqual(ratio_validity(Decimal(-3), Decimal(10)), "VALID")      # 合法负利润保留

    @case("G03-11")
    def test_unpublished_period_not_missing(self):
        t = sh_dt(date(2025, 8, 20), time(18))
        recs = [StatementRecord("X", "IS", date(2025, 6, 30), "CONS", "np", Decimal(3), t)]
        cutoff = sh_dt(date(2025, 10, 31), time(20))            # Q3 尚未公告
        self.assertEqual(latest_visible_period(recs, "X", "IS", "CONS", "np", cutoff), date(2025, 6, 30))

    @case("G03-12")
    def test_freshness_15_months(self):
        pe = date(2024, 12, 31)
        self.assertTrue(financial_fresh(pe, date(2026, 3, 31)))
        self.assertFalse(financial_fresh(pe, date(2026, 4, 1)))
        self.assertFalse(financial_fresh(pe, date(2026, 1, 1), max_months=12))
        with self.assertRaises(ContractError):
            financial_fresh(pe, date(2026, 1, 1), max_months=18)

    @case("G03-13")
    def test_naive_time_rejected(self):
        with self.assertRaises(ContractError) as cm:
            C.parse_ts("2026-01-05T10:00:00")
        self.assertEqual(cm.exception.code, "TIMEZONE_MISSING")

    @case("G03-14")
    def test_holiday_week_end(self):
        ends = self.cal.week_ends()
        self.assertIn(date(2026, 4, 2), ends)          # 周五 4/3 为假日 → 周四为周末截止
        self.assertNotIn(date(2026, 4, 3), ends)

    @case("G03-15")
    def test_incomplete_week_not_cutoff(self):
        ends = self.cal.week_ends(data_end=date(2026, 1, 7))      # 周三数据终点
        self.assertNotIn(date(2026, 1, 9), ends)
        self.assertEqual(max(ends), date(2026, 1, 2))
        short = TradingCalendar([d for d in self.cal.sessions if d <= date(2026, 1, 7)], known_through=date(2026, 1, 7))
        self.assertEqual(max(short.week_ends()), date(2026, 1, 2))

    @case("G03-16")
    def test_10am_fact_not_in_0930_proxy(self):
        mon = date(2026, 1, 5)
        self.assertEqual(self.cal.first_proxy_after(sh_dt(mon, time(10))), sh_dt(date(2026, 1, 6), time(9, 30)))
        self.assertEqual(self.cal.first_proxy_after(sh_dt(mon, time(9, 29, 59))), sh_dt(mon, time(9, 30)))

    @case("G03-17")
    def test_record_after_same_day_fill(self):
        lg, ev = new_ledger("100000")
        lg.apply(ev("ORDER_CREATE", buy_order("o1", qty=200, limit="10", trade_date="2026-01-05")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "o1", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 200, "10", at="2026-01-05T14:59:00+08:00")))
        lg.apply(ev("CA_RECORD", {"action_id": "D1", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                                  "record_date": "2026-01-05", "cash_per_share": "0.5"}))
        self.assertEqual(lg.entitlements["D1"]["eligible_qty"], 200)

    @case("G03-18")
    def test_late_corporate_action_reprojection(self):
        from smallcap.account import reproject
        from tests.fixtures import test_fees
        boot = {"event_id": "boot", "kind": "BOOTSTRAP_ACCOUNT", "effective_key": "2026-01-02T00", "payload": {
            "cash": "1000", "positions": [{"security_id": "X0001.SH", "market": "SH", "board": "MAIN_SH",
                                           "quantity": 100, "mark": "10", "acquisition_tax_date": "2025-01-02"}]}}
        sell = {"event_id": "sell-o", "kind": "ORDER_CREATE", "effective_key": "2026-01-06T10:00", "payload": {
            "order": {"client_order_id": "s1", "batch_id": "b", "plan_id": "p", "security_id": "X0001.SH", "market": "SH",
                      "board": "MAIN_SH", "side": "SELL", "qty": 100, "limit": "9", "kind": "ORDINARY",
                      "trade_date": "2026-01-06", "capital_revision": 1, "window_end": "x", "upper_price_bound": "11"}}}
        sub = {"event_id": "sell-s", "kind": "ORDER_SUBMIT", "effective_key": "2026-01-06T10:00:01",
               "payload": {"client_order_id": "s1", "at": "x"}}
        fil = {"event_id": "sell-f", "kind": "BROKER_FILL", "effective_key": "2026-01-06T10:00:02",
               "payload": fill("s1", "T-1", 100, "9", trade_date="2026-01-06")}
        facts = [boot, sell, sub, fil]
        old, _ = reproject("A", facts, "R1", test_fees())
        late = [{"event_id": "rec", "kind": "CA_RECORD", "effective_key": "2026-01-05T20", "payload": {
                    "action_id": "D1", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND", "record_date": "2026-01-05",
                    "cash_per_share": "1"}},
                {"event_id": "ex", "kind": "CA_EX", "effective_key": "2026-01-06T09", "payload": {"action_id": "D1"}}]
        new, info = reproject("A", facts + late, "R2", test_fees())
        # 实际成交不变：仍卖出100股@9；修正投影增加应收100，不倒造订单
        self.assertEqual(old.trade_log, new.trade_log)
        self.assertEqual(new.receivables["DIV:D1"], Decimal(100))
        self.assertEqual(new.entitlements["D1"]["eligible_qty"], 100)
        self.assertEqual(new.cash - old.cash, 0)
        self.assertEqual(new.projection_revision, "R2")
        self.assertEqual(info["applied_order"][:3], ["boot", "rec", "ex"])


if __name__ == "__main__":
    unittest.main()
