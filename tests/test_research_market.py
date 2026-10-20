"""研究数据仓适配器与真实数据接入相关的引擎行为（2026-10-19 因子环节新增）。

- 财报只有公告日期：自其后首个市场日 09:30 起可用；TTM=当期累计+上年全年−上年同期累计；缺依赖 → 来源未知；超 15 个月 → STALE。
- 惰性池（按市值顺序评估）与全量评估结果相同。
- 停牌日除权：按条款显式换算参考价（D12.3），停牌期间不被旧收盘覆盖。
- 零碎股 FLOOR_ODD_LOT_LOTTERY：向下取整、不补偿、不阻断。
"""
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

try:
    import numpy as np
except ImportError:                                   # 生产核心不依赖 numpy；无 numpy 环境跳过本组
    np = None

from smallcap.contracts import sh_dt
from smallcap.factors import SOURCE_UNKNOWN, STALE, VALID
from tests.fixtures import EventFactory, StubMarket, close_day, make_runner, new_ledger, seed_holdings, stub_tax_rules, test_fees

TZ = timezone(timedelta(hours=8))


def mini_store(d: Path, dates, codes, close, mv_wan, statements, actions=(), status=None):
    n, m = len(dates), len(codes)
    meta = {"purpose": "TEST_ONLY", "sessions": dates, "known_through": dates[-1], "data_end": dates[-1],
            "dates": dates, "codes": codes, "exchange": [c.split(".")[1] for c in codes],
            "board": ["MAIN_SH" if c.endswith("SH") else "MAIN_SZ" for c in codes], "list_date": ["20000101"] * m,
            "delist_date": [""] * m, "financial_codes": ["801780.SI"]}
    (d / "meta.json").write_text(json.dumps(meta))
    st = np.ones((n, m), dtype=np.int8) if status is None else np.array(status, dtype=np.int8)
    arrs = {"open": close, "high": close, "low": close, "close": close, "pre_close": close,
            "amount": np.full((n, m), 5e7), "volume": np.full((n, m), 1e6), "mv_wan": mv_wan,
            "free_share": np.full((n, m), 5e7), "status": st}
    for k, v in arrs.items():
        np.save(d / f"{k}.npy", np.asarray(v))
    (d / "risk.json").write_text("{}")
    (d / "industry.json").write_text(json.dumps({c: [["19900101", None, "801010.SI"]] for c in codes}))
    (d / "actions.json").write_text(json.dumps(list(actions)))
    (d / "lifecycle.json").write_text("[]")
    (d / "statements.json").write_text(json.dumps(statements))


def weekdays(a: date, n: int):
    out, x = [], a
    while len(out) < n:
        if x.weekday() < 5:
            out.append(x.strftime("%Y%m%d"))
        x += timedelta(days=1)
    return out


@unittest.skipIf(np is None, "需要 numpy（研究适配器可选依赖）")
class ResearchStore(unittest.TestCase):
    def test_ttm_availability_and_staleness(self):
        """2024-04-26（周五）公告的一季报 + 上年年报 + 上年一季报 → TTM；当晚 20:00 不可见，下一市场日 09:30 后可见。"""
        from smallcap.research_market import ResearchMarket
        dates = weekdays(date(2024, 4, 1), 60)
        with tempfile.TemporaryDirectory() as t:
            stm = {"A.SH": [["20230331", "20230425", "100"], ["20231231", "20240320", "500"],
                            ["20240331", "20240426", "160"]],
                   "B.SH": [["20221231", "20230320", "50"]]}
            mini_store(Path(t), dates, ["A.SH", "B.SH"], np.full((60, 2), 10.0), np.full((60, 2), 1e5), stm)
            m = ResearchMarket(t)
            fri = sh_dt(date(2024, 4, 26), time(20))
            val, st, at = m._np_ttm("A.SH", fri)
            self.assertEqual((val, st), (Decimal("500"), VALID))          # 一季报尚不可见：仍用年报
            val, st, at = m._np_ttm("A.SH", sh_dt(date(2024, 4, 29), time(9, 30)))
            self.assertEqual((val, st), (Decimal("560"), VALID))          # 160+500−100
            self.assertEqual(at, sh_dt(date(2024, 4, 29), time(9, 30)))
            val, st, _ = m._np_ttm("B.SH", sh_dt(date(2024, 4, 26), time(20)))
            self.assertEqual(st, STALE)                                   # 2022 年报超过 15 个月
            # 缺上年同期：来源未知
            stm2 = {"A.SH": [["20231231", "20240320", "500"], ["20240331", "20240426", "160"]]}
            mini_store(Path(t), dates, ["A.SH", "B.SH"], np.full((60, 2), 10.0), np.full((60, 2), 1e5), stm2)
            m2 = ResearchMarket(t)
            self.assertEqual(m2._np_ttm("A.SH", sh_dt(date(2024, 5, 6), time(20)))[1], SOURCE_UNKNOWN)

    def test_factor_inputs_windows(self):
        from smallcap.research_market import ResearchMarket
        dates = weekdays(date(2024, 1, 1), 70)
        st = np.ones((70, 1))
        st[10:15, 0] = 2                                                  # 5 天停牌
        with tempfile.TemporaryDirectory() as t:
            mini_store(Path(t), dates, ["A.SH"], np.full((70, 1), 10.0), np.full((70, 1), 1e5), {}, status=st)
            m = ResearchMarket(t)
            d = date.fromisoformat(f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}")
            fi = m.factor_inputs("A.SH", d, sh_dt(d, time(20)), {"daily_return_window_60", "turnover_window_60",
                                                                 "market_value_total"})
            win = fi["daily_return_window_60"][0]
            self.assertEqual((len(win), sum(x is None for x in win)), (60, 5))
            self.assertEqual(fi["market_value_total"][0], Decimal("1000000000.0"))
            self.assertEqual(fi["turnover_window_60"][0][-1], (Decimal("1000000.0"), Decimal("500000000000.0")))


class LazyPoolEquivalence(unittest.TestCase):
    """按 (市值, 代码) 顺序惰性评估的池与全量评估逐项一致（成员、边界、健康、不确定项）。"""

    def test_same_pool(self):
        import smallcap.replay as R
        from smallcap.pool import build_pool

        class Ordered(StubMarket):
            def mv_order(self, d):
                listed = [s for s in self.universe() if self.bar(s, d) is not None]
                return sorted(listed, key=lambda s: (self.bar(s, d)["mv"], s)), []
        d = date(2024, 1, 5)
        at = sh_dt(d, time(20))
        m = Ordered(n=60)
        for i, sid in enumerate(sorted(m.master)):
            m.master[sid]["shares"] = 10_000_000 + (59 - i) * 1000        # 编号越大市值越小
        for sid in ("X0060.SH", "X0058.SH"):                              # 最小的几只里有 ST，须跳过而不终止
            m.risk.append({"security_id": sid, "status": "ST", "effective": date(2023, 1, 2),
                           "available_at": sh_dt(date(2023, 1, 2), time(9))})
        r = make_runner(m)
        old = R.POOL_LIMIT
        try:
            R.POOL_LIMIT = 20
            lazy = r.pool_for(d, at)
        finally:
            R.POOL_LIMIT = old
        full = build_pool(f"{d.isoformat()}", d, [r.security_facts(s, d, at) for s in m.universe()], r.cal, r.rb,
                          set(r.spec.allowed_boards), set(r.spec.financial_codes), limit=20, used_at=at)
        for k in ("security_ids", "health", "uncertain", "size"):
            self.assertEqual(lazy[k], full[k], k)
        self.assertNotIn("X0060.SH", lazy["security_ids"])
        self.assertEqual(len(lazy["eligibility"]), 22)                   # 20 只合格 + 2 只 ST 后即停止评估
        self.assertEqual(R._POOL_CACHE.get((None, d, at)), None)          # 无 cache_token 的可变市场不进缓存


class SuspendedExRights(unittest.TestCase):
    def test_reference_conversion_and_kept_during_suspension(self):
        """停牌中除权：参考价=(最近收盘10−每股派现0.5)÷(1+每股送转0.5)=6.33（到分），标暂估；停牌期间收盘标记不被旧价覆盖。"""
        m = StubMarket(n=40)
        r = make_runner(m, fees=test_fees(tax_rules=stub_tax_rules()))
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 1000}, 90000)
        sid = "X0001.SH"
        for d in (date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10)):
            m.set_bar(sid, d, status="SUSPENDED")
        rec, ex = date(2024, 1, 5), date(2024, 1, 9)
        m.actions += [{"action_id": "c", "security_id": sid, "kind": "CASH_DIVIDEND", "record_date": rec, "ex_date": ex,
                       "pay_date": ex, "cash_per_share_cents": 50},
                      {"action_id": "s", "security_id": sid, "kind": "STOCK_DIVIDEND", "record_date": rec, "ex_date": ex,
                       "credit_date": ex, "listing_date": ex, "shares_per_share": "0.5",
                       "rounding": "FLOOR_ODD_LOT_LOTTERY", "tax_treatment": "NOT_TAXABLE"}]
        close_day(r, rec)                                                 # 登记
        r.phase_key, r.k, r._qc = "pre", 0, {}
        r._phase_preopen(ex)
        p = r.ledger.positions[sid]
        self.assertEqual((p.mark, p.mark_status), (Decimal("6.33"), "PROVISIONAL_REFERENCE"))
        self.assertEqual(p.quantity, 1500)
        close_day(r, ex)
        self.assertEqual((p.mark, p.mark_status), (Decimal("6.33"), "PROVISIONAL_REFERENCE"))
        self.assertFalse(any(k.startswith("CA_TERMS_UNKNOWN") for k in r.ledger.limitations))

    def test_odd_lot_lottery_floor(self):
        """101 股 10 送 3：应得 30.3 股 → 向下取整 30 股，不补偿、不产生 CA_TERMS_UNKNOWN 阻断。"""
        lg, ev = new_ledger(fees=test_fees(tax_rules=stub_tax_rules()))
        from tests.fixtures import buy_order, fill
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=101)))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "2026-01-05T10:00:00+08:00"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t1", 101, "10")))
        lg.apply(ev("CA_RECORD", {"action_id": "s", "security_id": "X0001.SH", "kind": "STOCK_DIVIDEND",
                                  "record_date": "2026-01-06", "shares_per_share": "0.3",
                                  "rounding": "FLOOR_ODD_LOT_LOTTERY", "tax_treatment": "NOT_TAXABLE"}))
        lg.apply(ev("CA_EX", {"action_id": "s", "ex_date": "2026-01-07", "reference_price": "7.69"}))
        self.assertEqual(lg.positions["X0001.SH"].share_receivable, 30)
        self.assertEqual(lg.entitlements["s"]["fraction_dropped"], "3/10")
        self.assertFalse(any(k.startswith("CA_TERMS_UNKNOWN") for k in lg.limitations))


if __name__ == "__main__":
    unittest.main()
