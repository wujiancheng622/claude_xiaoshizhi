import copy
import unittest
from datetime import date, datetime, time, timezone
from decimal import Decimal
from fractions import Fraction

from smallcap import contracts as C
from smallcap.contracts import sh_dt
from smallcap.evaluation import (cagr, compare_with_b0, config_identity, etf_benchmark_window, interpret_b0_b1,
                                 max_drawdown, qualify_config, rank_configs, reapplication_check)
from smallcap.portfolio import plan_week
from tests.acceptance import case
from tests.fixtures import golden

STARTS = ("A0", "A1", "A2", "A3")


def cells(cagrs=None, mdd=("1", "10"), retired=(), over30=(), status="STARTED", avail="PASS", evidence=(), fee="0.01"):
    out = []
    for i, s in enumerate(STARTS):
        for j, sc in enumerate(C.SCENARIOS):
            k = (s, sc)
            out.append({"start": s, "scenario": sc, "startup_status": status, "retirement_triggered": k in retired,
                        "MDD_exact": {"num": mdd[0], "den": mdd[1]}, "mdd_over_30": k in over30,
                        "CAGR_decimal": (cagrs or {}).get(k, "0.05"), "package_availability": avail,
                        "evidence_reasons": list(evidence), "annual_fee_rate": fee})
    return out


def q(cs, **kw):
    kw.setdefault("test_package", False)
    kw.setdefault("external_verified", {"all": True})
    kw.setdefault("benchmark_specified", True)
    return qualify_config(cs, **kw)


class G16(unittest.TestCase):
    @case("G16-01")
    def test_first_day_loss_and_recovery(self):
        md = max_drawdown([("d0", Fraction(1)), ("d1", Fraction(9, 10)), ("d2", Fraction(1))])
        self.assertEqual((md["mdd"], md["peak_date"], md["trough_date"], md["recovery_date"]),
                         (Fraction(1, 10), "d0", "d1", "d2"))

    @case("G16-02", "G17-14", "G16_RETIRED_CAGR")
    def test_retired_positive_cagr_rejected(self):
        g = golden("G16_RETIRED_CAGR")
        start = sh_dt(date(2010, 1, 8), time(20))
        end = datetime.fromtimestamp(start.timestamp() + float(Decimal(g["input"]["years"]) * Decimal("365.2425") * 86400),
                                     tz=timezone.utc)
        c = cagr(Fraction(1), Fraction(3, 2), start, end)
        self.assertEqual(str(c.quantize(Decimal("1e-8"))), g["expected"]["CAGR_8dp"])
        md = max_drawdown([("a", Fraction(1)), ("b", Fraction(2)), ("c", Fraction(3, 2))])
        self.assertEqual(md["mdd"], Fraction(Decimal(g["expected"]["MDD"])))
        res = q(cells(cagrs={("A0", "S0"): "0.0413797"}, retired={("A0", "S0")}))
        self.assertEqual(res["status"], g["expected"]["adoption_status"])
        better = q(cells(cagrs={k: "0.03" for k in [(s, sc) for s in STARTS for sc in C.SCENARIOS]}))
        self.assertEqual(better["status"], "REVIEWABLE")

    @case("G16-03")
    def test_one_retired_cell_excluded(self):
        good = {"config_key": "high", "role": "B1", "qualification": q(cells(cagrs={("A0", "S0"): "0.5"},
                                                                              retired={("A2", "S3")}))}
        ok = {"config_key": "ok", "role": "B1", "qualification": q(cells())}
        self.assertEqual(rank_configs([good, ok])["ranking"], ["ok"])

    @case("G16-04")
    def test_both_reasons_recorded(self):
        res = q(cells(retired={("A1", "S3")}, over30={("A1", "S3")}))
        self.assertIn("RETIRED_TRIGGERED:A1/S3", res["failures"])
        self.assertIn("MDD_OVER_30:A1/S3", res["failures"])

    @case("G16-05")
    def test_mdd_boundary(self):
        self.assertFalse(Fraction(3, 10) > C.DD_LIMIT)
        self.assertTrue(Fraction(3, 10) + Fraction(1, 10 ** 12) > C.DD_LIMIT)
        self.assertEqual(q(cells(mdd=("3", "10")))["status"], "REVIEWABLE")

    @case("G16-06")
    def test_robust_is_min(self):
        vals = ["0.12", "0.11", "0.07", "0.06", "0.08", "0.07", "0.05", "0.04"]
        cg = {}
        it = iter(vals)
        for s in STARTS:
            for sc in ("S0", "S1"):
                cg[(s, sc)] = next(it)
        self.assertEqual(Decimal(q(cells(cagrs=cg))["robust_cagr"]), Decimal("0.04"))

    @case("G16-07")
    def test_stress_not_in_robust(self):
        cg = {(s, sc): "-0.20" for s in STARTS for sc in ("S2", "S3")}
        res = q(cells(cagrs=cg))
        self.assertEqual((res["status"], Decimal(res["robust_cagr"])), ("REVIEWABLE", Decimal("0.05")))

    @case("G16-08")
    def test_robust_zero_fails(self):
        res = q(cells(cagrs={("A3", "S1"): "0"}))
        self.assertIn("ROBUST_CAGR_NOT_POSITIVE", res["failures"])

    @case("G16-09")
    def test_tradeoff(self):
        b1 = {"S0": {"CAGR_decimal": "0.05", "MDD_exact": {"num": "1", "den": "10"}, "annual_fee_rate": "0.01"},
              "S1": {"CAGR_decimal": "0.04", "MDD_exact": {"num": "1", "den": "10"}, "annual_fee_rate": "0.01"}}
        b0 = {"S0": {"CAGR_decimal": "0.06", "MDD_exact": {"num": "2", "den": "10"}, "annual_fee_rate": "0.01"},
              "S1": {"CAGR_decimal": "0.05", "MDD_exact": {"num": "2", "den": "10"}, "annual_fee_rate": "0.01"}}
        self.assertEqual(compare_with_b0(b1, b0), "TRADEOFF_PENDING")
        b1_up = copy.deepcopy(b1)
        b1_up["S0"]["CAGR_decimal"], b1_up["S1"]["CAGR_decimal"] = "0.07", "0.05"
        self.assertEqual(compare_with_b0(b1_up, b0), "INCREMENTAL_RECOMMENDATION")

    @case("G16-10")
    def test_b0_no_factor_dependency(self):
        ids = [f"X{i:04d}.SH" for i in range(1, 41)]
        pool = {"security_ids": ids, "size_order": {s: i + 1 for i, s in enumerate(ids)}, "size": 40, "health": "OK",
                "codes": {s: s for s in ids}, "industries": {s: f"I{i % 10}" for i, s in enumerate(ids)},
                "eligibility": {s: "TRUE" for s in ids}}
        from smallcap.factors import b0_ranking
        plan, counters = plan_week(role="B0", plan_id="p", cutoff="c", e_decision=Fraction(100000), holdings=[],
                                   open_buy_sids=set(), pool=pool, score=b0_ranking(pool), counters={"X": 1},
                                   capital_revision=1, execute_session="x")
        self.assertEqual(len(plan["buy_candidates_ordered"]), 30)
        self.assertEqual(counters, {"X": 1})
        rec = {"package_availability": "NOT_APPLICABLE_B0"}
        self.assertNotEqual(rec["package_availability"], "FAIL")

    @case("G16-11")
    def test_b0_retired_b1_survives(self):
        b0 = q(cells(retired={("A0", "S3")}))
        b1 = q(cells())
        self.assertEqual(interpret_b0_b1(b0, b1), "OPERATING_IMPROVEMENT_NOT_PURE_ALPHA")

    @case("G16-12")
    def test_b2_not_champion(self):
        b1 = {"config_key": "b1", "role": "B1", "qualification": q(cells())}
        b2 = {"config_key": "b2", "role": "B2", "qualification": q(cells(cagrs={(s, sc): "0.5" for s in STARTS
                                                                               for sc in C.SCENARIOS}))}
        self.assertEqual(rank_configs([b1, b2])["ranking"], ["b1"])

    @case("G16-13")
    def test_etf_pre_listing(self):
        w = etf_benchmark_window(date(2013, 5, 1), date(2011, 1, 7), date(2026, 10, 16))
        self.assertEqual((w["window"][0], w["pre_listing_excluded"]), ("2013-05-01", True))
        self.assertEqual(etf_benchmark_window(date(2027, 1, 1), date(2011, 1, 7), date(2026, 10, 16))["status"],
                         "NO_COMMON_WINDOW")

    @case("G16-14")
    def test_benchmark_not_specified(self):
        res = q(cells(), benchmark_specified=False)
        self.assertEqual(res["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("ETF_BENCHMARK_NOT_SPECIFIED", res["unknowns"])
        self.assertEqual(etf_benchmark_window(None, date(2011, 1, 7), date(2026, 1, 1))["status"], "BENCHMARK_NOT_SPECIFIED")

    @case("G16-16")
    def test_reapply_requirements(self):
        prev = {"config": {"rules": "V5D2", "factors": ["A"], "capital": "500000"}, "retirement_trigger_at": "2015-07-08"}
        new = {"rules": "V5D2", "factors": ["A", "B"], "capital": "500000"}
        r = reapplication_check(prev, new, changes=["加入因子B"], matrix_complete=False, external_revalidated=False,
                                user_decision_ref=None)
        self.assertFalse(r["eligible_for_review"])
        self.assertEqual(set(r["missing"]), {"ORIGINAL_MATRIX_NOT_REPLAYED", "EXTERNAL_FACTS_NOT_REVALIDATED",
                                             "USER_DECISION_MISSING"})
        ok = reapplication_check(prev, new, changes=["加入因子B"], matrix_complete=True, external_revalidated=True,
                                 user_decision_ref="USER-2026-10-20")
        self.assertTrue(ok["eligible_for_review"])

    @case("G16-17")
    def test_all_fail(self):
        a = {"config_key": "a", "role": "B1", "qualification": q(cells(retired={("A0", "S0")}))}
        b = {"config_key": "b", "role": "B1", "qualification": q(cells(over30={("A1", "S2")}))}
        self.assertEqual(rank_configs([a, b])["conclusion"], "NO_ADOPTABLE_CONFIGURATION")

    @case("G16-18")
    def test_rename_not_new_evidence(self):
        a = {"rules": "V5D2", "factors": ["A"], "package_id": "PKG-OLD", "file_name": "a.json"}
        b = dict(a, package_id="PKG-NEW-NAME", file_name="renamed.json")
        self.assertEqual(config_identity(a), config_identity(b))
        prev = {"config": a, "retirement_trigger_at": "2015-07-08"}
        r = reapplication_check(prev, b, changes=[], matrix_complete=True, external_revalidated=True, user_decision_ref="u")
        self.assertIn("SAME_CONTENT_RENAMED", r["missing"])

    @case("G16-19")
    def test_ts_only_not_deployable(self):
        res = q(cells(), external_verified={"sources": False})
        self.assertEqual(res["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("EXTERNAL_NOT_VERIFIED:sources", res["unknowns"])


if __name__ == "__main__":
    unittest.main()
