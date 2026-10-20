"""投资候选因子实现的手算黄金例（H03.2 golden_examples）：负值、缺失、未来依赖、观测不足、并列与合成。

期望值全部手算（注释给出算式），不调用被测实现生成。golden_case_ids 与 profiles/factor_packages/*.json 对应。
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import smallcap  # noqa: F401  触发登记
from smallcap.bundle import schema_errors
from smallcap.factors import (MODEL_INAPPLICABLE, REGISTRY, SOURCE_UNKNOWN, STALE, VALID, compute_factor,
                              score_package, validate_package)

TZ = timezone(timedelta(hours=8))
CUT = datetime(2024, 1, 5, 20, 0, tzinfo=TZ)
OK_AT = datetime(2024, 1, 5, 15, 0, tzinfo=TZ)
FUTURE = datetime(2024, 1, 8, 9, 30, tzinfo=TZ)
ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "profiles" / "factor_packages"


def spec(impl, fid, direction, quantum):
    return {"factor_id": fid, "implementation_id": impl, "direction": direction, "output_unit": "x",
            "output_quantum": quantum, "input_fields": ["x"], "formula_definition": "x", "validity_rules": ["x"],
            "max_report_age_months": 15, "golden_case_ids": ["x"], "source_scope_ref": "x"}


SIZE = spec("SIZE_TOTAL_MV_V1", "SIZE", "LOWER_BETTER", "1")
EP = spec("EP_TTM_PARENT_V1", "EP", "HIGHER_BETTER", "0.00000001")
VOL = spec("VOL60_DAILY_RETURN_STD_V1", "VOL60", "LOWER_BETTER", "0.00000001")
TURN = spec("TURN60_FREE_FLOAT_V1", "TURN60", "LOWER_BETTER", "0.00000001")


def v(x, st=VALID, at=OK_AT):
    return (x, st, at)


class FactorImplGolden(unittest.TestCase):
    def test_registered(self):
        for impl in ("SIZE_TOTAL_MV_V1", "EP_TTM_PARENT_V1", "VOL60_DAILY_RETURN_STD_V1", "TURN60_FREE_FLOAT_V1"):
            self.assertIn(impl, REGISTRY)

    def test_fi_size_01(self):
        """FI-SIZE-01：总市值 1234567890.50 元按量化 1 元 HALF_UP → 1234567891；0 → 模型不适用；缺失/未来 → 来源未知。"""
        facts = {"A": {"market_value_total": v(Decimal("1234567890.50"))},
                 "B": {"market_value_total": v(Decimal("0"))},
                 "C": {},
                 "D": {"market_value_total": v(Decimal("1"), at=FUTURE)}}
        out = compute_factor(SIZE, ["A", "B", "C", "D"], CUT, facts)
        self.assertEqual((out["A"].status, out["A"].value), (VALID, Decimal("1234567891")))
        self.assertEqual(out["B"].status, MODEL_INAPPLICABLE)
        self.assertEqual(out["C"].status, SOURCE_UNKNOWN)
        self.assertEqual((out["D"].status, out["D"].reason), (SOURCE_UNKNOWN, "market_value_total_FUTURE_DEPENDENCY"))

    def test_fi_ep_01(self):
        """FI-EP-01：TTM −5,000,000 ÷ 市值 500,000,000 = −0.01（亏损保留负值）；8,000,000÷400,000,000=0.02；
        市值≤0 → 模型不适用；TTM 状态 STALE 原样传出。"""
        facts = {"A": {"np_parent_ttm": v(Decimal("-5000000")), "market_value_total": v(Decimal("500000000"))},
                 "B": {"np_parent_ttm": v(Decimal("8000000")), "market_value_total": v(Decimal("400000000"))},
                 "C": {"np_parent_ttm": v(Decimal("1")), "market_value_total": v(Decimal("-1"))},
                 "D": {"np_parent_ttm": v(None, STALE), "market_value_total": v(Decimal("1"))}}
        out = compute_factor(EP, ["A", "B", "C", "D"], CUT, facts)
        self.assertEqual((out["A"].status, out["A"].value), (VALID, Decimal("-0.01000000")))
        self.assertEqual(out["B"].value, Decimal("0.02000000"))
        self.assertEqual(out["C"].status, MODEL_INAPPLICABLE)
        self.assertEqual(out["D"].status, STALE)

    def test_fi_vol_01(self):
        """FI-VOL-01：60 日窗口内 40 个有效日收益 ±1% 各 20 个、20 个停牌：均值 0，样本方差 40×0.0001/39，
        标准差 sqrt(4/39000)=0.0101273936708…→ 0.01012739；39 个有效日 → 模型不适用；窗口长度不是 60 → 来源未知。"""
        up, dn = (Decimal("10.10"), Decimal("10.00")), (Decimal("9.90"), Decimal("10.00"))
        win = [up, dn] * 20 + [None] * 20
        short = [up, dn] * 19 + [up] + [None] * 21
        facts = {"A": {"daily_return_window_60": v(win)}, "B": {"daily_return_window_60": v(short)},
                 "C": {"daily_return_window_60": v(win[:59])}}
        out = compute_factor(VOL, ["A", "B", "C"], CUT, facts)
        self.assertEqual((out["A"].status, out["A"].value), (VALID, Decimal("0.01012739")))
        self.assertEqual((out["B"].status, out["B"].reason), (MODEL_INAPPLICABLE, "INSUFFICIENT_TRADING_DAYS_39"))
        self.assertEqual(out["C"].status, SOURCE_UNKNOWN)

    def test_fi_turn_01(self):
        """FI-TURN-01：50 个有效日 成交 1,000,000 股 ÷ 自由流通 50,000,000 股 = 0.02，均值 0.02；自由流通 0 → 模型不适用。"""
        win = [(Decimal(1000000), Decimal(50000000))] * 50 + [None] * 10
        bad = [(Decimal(1000000), Decimal(0))] + [(Decimal(1), Decimal(10))] * 59
        out = compute_factor(TURN, ["A", "B"], CUT, {"A": {"turnover_window_60": v(win)},
                                                    "B": {"turnover_window_60": v(bad)}})
        self.assertEqual((out["A"].status, out["A"].value), (VALID, Decimal("0.02000000")))
        self.assertEqual(out["B"].status, MODEL_INAPPLICABLE)

    def test_fi_pkg_01(self):
        """FI-PKG-01：四只股票 SIZE/EP/VOL60 合成（平均并列百分位，等权）：
        SIZE 越小越好 A1e9 B2e9 C3e9 D4e9 → A 1、B 1/3、C −1/3、D −1；
        EP 越大越好 A −0.01、B=C 0.05、D 0.10 → A −1、B=C 0、D 1；
        VOL 越小越好 A0.04 B0.02 C0.03 D0.01 → A −1、B 1/3、C −1/3、D 1；
        S=(和)/3：A −1/3、B 2/9、C −2/9、D 1/3；名次 D、B、C、A。"""
        facts = {
            "A": {"market_value_total": v(Decimal(10 ** 9)), "np_parent_ttm": v(Decimal("-10000000"))},
            "B": {"market_value_total": v(Decimal(2 * 10 ** 9)), "np_parent_ttm": v(Decimal("100000000"))},
            "C": {"market_value_total": v(Decimal(3 * 10 ** 9)), "np_parent_ttm": v(Decimal("150000000"))},
            "D": {"market_value_total": v(Decimal(4 * 10 ** 9)), "np_parent_ttm": v(Decimal("400000000"))},
        }
        vols = {"A": "0.04", "B": "0.02", "C": "0.03", "D": "0.01"}
        ids = ["A", "B", "C", "D"]
        from smallcap.factors import FactorValue
        fv = {"SIZE": compute_factor(SIZE, ids, CUT, facts), "EP": compute_factor(EP, ids, CUT, facts),
              "VOL60": {s: FactorValue(Decimal(vols[s]), VALID) for s in ids}}
        pkg = {"package_id": "T", "purpose": "TEST_ONLY", "factors": [SIZE, EP, VOL]}
        pool = {"security_ids": ids, "health": "OK"}
        res = score_package(pkg, pool, fv, {s: s for s in ids})
        self.assertEqual(res["score_fraction"], {"A": Fraction(-1, 3), "B": Fraction(2, 9), "C": Fraction(-2, 9),
                                                 "D": Fraction(1, 3)})
        self.assertEqual(res["order"], ["D", "B", "C", "A"])


class PackageProfiles(unittest.TestCase):
    """profiles/factor_packages 下的候选包：Schema 合法、实现已登记、黄金例编号存在于本文件。"""

    def test_packages_valid(self):
        files = sorted(PKG_DIR.glob("*.json"))
        self.assertTrue(files)
        here = Path(__file__).read_text(encoding="utf-8")
        for f in files:
            prof = json.loads(f.read_text(encoding="utf-8"))
            pkg = prof["payload"]
            validate_package(pkg)
            for fs in pkg["factors"]:
                self.assertIn(fs["implementation_id"], REGISTRY, f.name)
                for gid in fs["golden_case_ids"]:
                    self.assertIn(gid, here, f"{f.name}: {gid}")
            bundle = json.loads((ROOT / "examples" / "engineering_synthetic_bundle.json").read_text(encoding="utf-8"))
            bundle["profiles"]["factor"] = prof
            self.assertEqual(schema_errors(bundle), [], f.name)


if __name__ == "__main__":
    unittest.main()
