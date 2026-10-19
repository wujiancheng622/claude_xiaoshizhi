import copy
import random
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

from smallcap import contracts as C
from smallcap.factors import (FactorValue, b0_ranking, compute_factor, package_availability, percentile_scores,
                              score_package, update_missing_counters)
from smallcap.pool import FALSE, TRUE, UNK, SecurityFacts, adv20, build_pool, eligibility
from smallcap.portfolio import HoldingView, plan_week
from smallcap.execution import ordinary_sell_qty
from smallcap.rules import RuleBook
from tests.acceptance import case
from tests.fixtures import ENGINEERING_BUNDLE, golden, test_rulebook, test_rulebook_payload, trade_rule, weekday_calendar

CAL = weekday_calendar(date(2018, 1, 1), date(2027, 12, 31))
D0 = date(2026, 1, 9)
RB = test_rulebook()
BOARDS = {"MAIN_SH", "MAIN_SZ"}
FIN = {"SW_BANK", "SW_NONBANK"}


def sf(i, mv=None, **kw):
    amounts = [(d, Decimal(20_000_000), "TRADING") for d in CAL.last_n_sessions(D0, 20)]
    base = dict(security_id=f"X{i:04d}.SH", trade_code=f"X{i:04d}.SH", exchange="SH", board="MAIN_SH",
                list_date=date(2018, 1, 1), industry_sw1=f"SW{i % 20:02d}", raw_close=Decimal(10),
                amounts_20=amounts, market_value_total=Decimal(mv if mv is not None else 1_000_000_000 + i * 1000))
    base.update(kw)
    return SecurityFacts(**base)


def pool_of(facts, rb=RB):
    return build_pool("d", D0, facts, CAL, rb, BOARDS, FIN)


def pkg(*dirs):
    p = copy.deepcopy(ENGINEERING_BUNDLE["profiles"]["factor"]["payload"])
    f0 = p["factors"][0]
    p["factors"] = [dict(f0, factor_id=f"F{i}", direction=d) for i, d in enumerate(dirs)]
    return p


def fv(values):
    return {s: (FactorValue(Decimal(v), "VALID") if v is not None else FactorValue(None, "SOURCE_UNKNOWN"))
            for s, v in values.items()}


class G04(unittest.TestCase):
    @case("G04-01")
    def test_400_smallest_no_factor(self):
        facts = [sf(i) for i in range(500)]
        p = pool_of(facts)
        self.assertEqual(p["size"], 400)
        self.assertEqual(p["health"], "OK")
        self.assertEqual(p["security_ids"], [f"X{i:04d}.SH" for i in range(400)])

    @case("G04-02")
    def test_tie_by_code_and_shuffle(self):
        facts = [sf(i) for i in range(398)] + [sf(900, mv=5_000_000_000), sf(901, mv=5_000_000_000), sf(902, mv=5_000_000_000)]
        a = pool_of(facts)
        self.assertEqual(a["security_ids"][-2:], ["X0900.SH", "X0901.SH"])
        rng = random.Random(3)
        for _ in range(10):
            f2 = facts[:]
            rng.shuffle(f2)
            self.assertEqual(pool_of(f2)["security_ids"], a["security_ids"])

    @case("G04-03")
    def test_later_delisted_kept(self):
        f = sf(1)
        f.listed = True           # 当时上市；后来退市的事实不提前进入
        self.assertEqual(eligibility(f, D0, CAL, RB, BOARDS, FIN)[0], TRUE)

    @case("G04-04")
    def test_listing_age(self):
        lst = CAL.sessions[CAL.index(D0) - 249]
        self.assertEqual(eligibility(sf(1, list_date=lst), D0, CAL, RB, BOARDS, FIN)[1]["listing_age"], FALSE)
        lst = CAL.sessions[CAL.index(D0) - 250]
        self.assertEqual(eligibility(sf(1, list_date=lst), D0, CAL, RB, BOARDS, FIN)[1]["listing_age"], TRUE)
        early = CAL.sessions[0] - __import__("datetime").timedelta(days=30)      # 日历未覆盖上市日
        self.assertEqual(eligibility(sf(1, list_date=early), CAL.sessions[100], CAL, RB, BOARDS, FIN)[1]["listing_age"], UNK)

    @case("G04-05")
    def test_price_floor(self):
        self.assertEqual(eligibility(sf(1, raw_close=Decimal("1.99")), D0, CAL, RB, BOARDS, FIN)[0], FALSE)
        self.assertEqual(eligibility(sf(1, raw_close=Decimal("2.00")), D0, CAL, RB, BOARDS, FIN)[0], TRUE)

    @case("G04-06")
    def test_adv_floor(self):
        for amt, exp in ((Decimal(9_999_999), FALSE), (Decimal(10_000_000), TRUE)):
            a = [(d, amt, "TRADING") for d in CAL.last_n_sessions(D0, 20)]
            self.assertEqual(eligibility(sf(1, amounts_20=a), D0, CAL, RB, BOARDS, FIN)[1]["liquidity"], exp)

    @case("G04-07")
    def test_suspended_day_zero(self):
        days = CAL.last_n_sessions(D0, 20)
        a = [(d, Decimal(20_000_000), "TRADING") for d in days[:19]] + [(days[19], None, "SUSPENDED")]
        self.assertEqual(adv20(a), Decimal(19 * 20_000_000) / 20)

    @case("G04-08")
    def test_missing_day_unknown(self):
        days = CAL.last_n_sessions(D0, 20)
        a = [(d, Decimal(20_000_000), "TRADING") for d in days[:19]] + [(days[19], None, "MISSING")]
        self.assertIsNone(adv20(a))
        self.assertEqual(eligibility(sf(1, amounts_20=a), D0, CAL, RB, BOARDS, FIN)[1]["liquidity"], UNK)

    @case("G04-09")
    def test_floor_not_applicable(self):
        self.assertEqual(eligibility(sf(1, market_value_total=Decimal(1)), D0, CAL, RB, BOARDS, FIN)[1]["market_value"], TRUE)
        floor_rb = RuleBook(test_rulebook_payload())
        floor_rb.rules[0] = floor_rb.rules[0].__class__(**{**floor_rb.rules[0].__dict__, "marketcap_floor": Decimal(300_000_000),
                                                            "marketcap_floor_status": "VALUE"})
        self.assertEqual(eligibility(sf(1, market_value_total=Decimal(200_000_000)), D0, CAL, floor_rb, BOARDS, FIN)[1]["market_value"], FALSE)

    @case("G04-10")
    def test_floor_rule_missing_unknown(self):
        p = test_rulebook_payload()
        p["rules"] = [r for r in p["rules"] if r["board"] != "MAIN_SH"] + [trade_rule("OLD", end="2020-01-01")]
        self.assertEqual(eligibility(sf(1), D0, CAL, RuleBook(p), BOARDS, FIN)[1]["market_value"], UNK)

    @case("G04-11")
    def test_399_plus_unknown_unhealthy(self):
        facts = [sf(i) for i in range(399)] + [sf(999, industry_sw1=None)]
        p = pool_of(facts)
        self.assertEqual(p["health"], "POOL_UNCERTAIN")
        self.assertEqual(p["uncertain"], ["X0999.SH"])

    @case("G04-12")
    def test_unknown_above_boundary_irrelevant(self):
        facts = [sf(i) for i in range(400)] + [sf(999, mv=9_000_000_000, industry_sw1=None)]
        p = pool_of(facts)
        self.assertEqual(p["health"], "OK")
        self.assertEqual(p["irrelevant_unknown"], ["X0999.SH"])

    @case("G04-13")
    def test_unknown_possibly_small(self):
        facts = [sf(i) for i in range(400)] + [sf(999, market_value_total=None)]
        self.assertEqual(pool_of(facts)["health"], "POOL_UNCERTAIN")
        facts = [sf(i) for i in range(400)] + [sf(999, mv=1, industry_sw1=None)]
        self.assertEqual(pool_of(facts)["health"], "POOL_UNCERTAIN")

    @case("G04-14")
    def test_confirmed_false_dominates_unknown(self):
        f = sf(999, raw_close=Decimal("1.50"), industry_sw1=None, market_value_total=None)
        self.assertEqual(eligibility(f, D0, CAL, RB, BOARDS, FIN)[0], FALSE)
        p = pool_of([sf(i) for i in range(400)] + [f])
        self.assertEqual(p["health"], "OK")

    @case("G04-15")
    def test_no_refill_beyond_400(self):
        facts = [sf(i) for i in range(480)]
        p = pool_of(facts)
        vals = {s: (None if i < 80 else str(i)) for i, s in enumerate(p["security_ids"])}
        sc = score_package(pkg("HIGHER_BETTER"), p, {"F0": fv(vals)}, p["codes"])
        self.assertEqual(sc["valid_count"], 320)
        self.assertTrue(set(sc["joint_ids"]) <= set(p["security_ids"]))
        self.assertEqual(sc["pool_count"], 400)

    @case("G04-16")
    def test_empty_pool(self):
        p = pool_of([sf(i, raw_close=Decimal(1)) for i in range(10)])
        self.assertEqual(p["health"], "EMPTY_ECONOMIC_POOL")
        sc = score_package(pkg("HIGHER_BETTER"), p, {"F0": {}}, p["codes"])
        self.assertIsNone(sc["coverage"])
        self.assertFalse(sc["healthy"])
        self.assertEqual(sc["reason"], "EMPTY_ECONOMIC_POOL")


def simple_pool(n):
    ids = [f"X{i:04d}.SH" for i in range(n)]
    return {"security_ids": ids, "size_order": {s: i + 1 for i, s in enumerate(ids)}, "size": n, "health": "OK",
            "codes": {s: s for s in ids}, "industries": {s: f"I{i % 10:02d}" for i, s in enumerate(ids)},
            "eligibility": {s: "TRUE" for s in ids}}


class G05(unittest.TestCase):
    @case("G05-01")
    def test_golden_percentile(self):
        g = golden("G05-01")
        vals = {f"s{i}": Decimal(v) for i, v in enumerate(g["input"]["values"])}
        out = percentile_scores(vals, g["input"]["direction"])
        self.assertEqual([C.wire_ratio(out[f"s{i}"]) for i in range(4)], g["expected"]["score_fractions"])

    @case("G05-02")
    def test_reverse_once(self):
        vals = {f"s{i}": Decimal(v) for i, v in enumerate(["10", "20", "20", "40"])}
        out = percentile_scores(vals, "LOWER_BETTER")
        self.assertEqual([out[f"s{i}"] for i in range(4)], [1, 0, 0, -1])

    @case("G05-03")
    def test_constant_factor_zero_keeps_weight(self):
        p = simple_pool(4)
        ids = p["security_ids"]
        sc = score_package(pkg("HIGHER_BETTER", "HIGHER_BETTER"), p,
                           {"F0": fv({s: "7" for s in ids}), "F1": fv(dict(zip(ids, ["1", "2", "3", "4"])))}, p["codes"])
        self.assertEqual([sc["score_fraction"][s] for s in ids], [Fraction(-1, 2), Fraction(-1, 6), Fraction(1, 6), Fraction(1, 2)])

    @case("G05-04")
    def test_single_valid(self):
        self.assertEqual(percentile_scores({"a": Decimal(5)}, "HIGHER_BETTER"), {"a": Fraction(0)})

    @case("G05-05", "G19-20")
    def test_flat_score_reduce_only(self):
        p = simple_pool(400)
        ids = p["security_ids"]
        vals = {s: str(i) for i, s in enumerate(ids)}
        sc = score_package(pkg("HIGHER_BETTER", "LOWER_BETTER"), p, {"F0": fv(vals), "F1": fv(vals)}, p["codes"])
        self.assertTrue(sc["flat"])
        E = Fraction(100000)
        holds = [HoldingView(ids[100], 1300, Fraction(2600), "I00"), HoldingView(ids[300], 700, Fraction(1400), "I00")]
        plan, _ = plan_week(role="B1", plan_id="p", cutoff="c", e_decision=E, holdings=holds, open_buy_sids=set(), pool=p,
                            score=sc, counters={}, capital_revision=1, execute_session="2026-01-12")
        self.assertEqual(plan["buy_candidates_ordered"], [])
        self.assertFalse(plan["ordinary_buy_allowed"])
        m1 = plan["modes"][ids[100]]
        self.assertEqual((m1["mode"], m1["prohibit_buy"], m1["reason"]), ("TARGET_AMOUNT", True, "FLAT_SCORE_REDUCE_ONLY"))
        self.assertEqual(plan["modes"][ids[300]]["mode"], "HOLD_SHARES")          # 名次300也不按排名退出
        tgt = C.unwire_ratio(m1["target_amount"])
        g = golden("G19-20")
        self.assertEqual(ordinary_sell_qty(RB.rules[0].grid, g["input"]["qty"], Decimal(g["input"]["execution_price"]),
                                           tgt, 1300), g["expected"]["qty"])

    @case("G05-06")
    def test_coverage_boundary(self):
        p = simple_pool(400)
        ids = p["security_ids"]
        for nvalid, healthy in ((320, True), (319, False)):
            vals = {s: (str(i) if i < nvalid else None) for i, s in enumerate(ids)}
            sc = score_package(pkg("HIGHER_BETTER"), p, {"F0": fv(vals)}, p["codes"])
            self.assertEqual(sc["healthy"], healthy)
            self.assertEqual(sc["coverage"], Fraction(nvalid, 400))

    @case("G05-07")
    def test_joint_intersection(self):
        p = simple_pool(400)
        ids = p["security_ids"]
        fvs = {f"F{k}": fv({s: (None if k * 40 <= i < (k + 1) * 40 else str(i)) for i, s in enumerate(ids)}) for k in range(3)}
        sc = score_package(pkg("HIGHER_BETTER", "HIGHER_BETTER", "HIGHER_BETTER"), p, fvs, p["codes"])
        self.assertEqual(sc["valid_count"], 280)
        self.assertEqual(sc["coverage"], Fraction(7, 10))
        self.assertFalse(sc["healthy"])

    @case("G05-08")
    def test_rank_on_common_set(self):
        p = simple_pool(400)
        ids = p["security_ids"]
        a = fv({s: str(i) for i, s in enumerate(ids)})
        b = fv({s: (str(1000 - i) if i < 320 else None) for i, s in enumerate(ids)})
        sc = score_package(pkg("HIGHER_BETTER", "HIGHER_BETTER"), p, {"F0": a, "F1": b}, p["codes"])
        self.assertEqual(sc["valid_count"], 320)
        # 两项都在同一 320 只上排名：互为反序 → 综合全0 → FLAT
        self.assertTrue(all(v == 0 for v in sc["score_fraction"].values()))

    @case("G05-09")
    def test_missing_one_item_excluded(self):
        p = simple_pool(5)
        ids = p["security_ids"]
        sc = score_package(pkg("HIGHER_BETTER", "HIGHER_BETTER"), p,
                           {"F0": fv({s: str(i) for i, s in enumerate(ids)}),
                            "F1": fv({s: (None if i == 2 else str(i)) for i, s in enumerate(ids)})}, p["codes"])
        self.assertNotIn(ids[2], sc["joint_ids"])
        self.assertIn(ids[2], sc["invalid_reasons"])

    @case("G05-10")
    def test_negative_and_extreme_kept(self):
        spec = dict(ENGINEERING_BUNDLE["profiles"]["factor"]["payload"]["factors"][0])
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        out = compute_factor(spec, ["a", "b", "c"], t, {"a": {"fixture_value": ("-5.25", "VALID", t)},
                                                         "b": {"fixture_value": ("1000000000", "VALID", t)},
                                                         "c": {"fixture_value": ("0", "VALID", t)}})
        self.assertEqual(out["a"].value, Decimal("-5.250000"))
        self.assertEqual(out["b"].value, Decimal("1000000000.000000"))
        s = percentile_scores({k: v.value for k, v in out.items()}, "HIGHER_BETTER")
        self.assertEqual((s["a"], s["c"], s["b"]), (-1, 0, 1))

    def _healthy(self, valid=None):
        p = simple_pool(400)
        return p, {"healthy": True, "score_fraction": {s: 0 for s in p["security_ids"] if s != "X0000.SH" or valid},
                   "ranks": {}, "invalid_reasons": {} if valid else {"X0000.SH": [("F0", valid or "SOURCE_UNKNOWN")]}}

    @case("G05-11")
    def test_global_fault_no_count_change(self):
        p, _ = self._healthy()
        c0 = {"X0000.SH": 1}
        c1, act = update_missing_counters(c0, ["X0000.SH"], False, None, p)
        c2, _ = update_missing_counters(c1, ["X0000.SH"], False, None, p)
        self.assertEqual(c2, c0)
        self.assertEqual(act["X0000.SH"], "GLOBAL_UNHEALTHY")

    @case("G05-12")
    def test_counter_sequence(self):
        p, missing = self._healthy()
        ok = {"healthy": True, "score_fraction": {"X0000.SH": 0}, "ranks": {"X0000.SH": 1}, "invalid_reasons": {}}
        seq = [(True, missing), (False, None), (False, None), (True, missing), (True, ok)]
        c, out, acts = {}, [], []
        for healthy, sc in seq:
            c, act = update_missing_counters(c, ["X0000.SH"], healthy, sc, p)
            out.append(c.get("X0000.SH", 0))
            acts.append(act["X0000.SH"])
        self.assertEqual(out, [1, 1, 1, 2, 0])
        self.assertEqual(acts[3], "EXIT_PERSISTENT")

    @case("G05-13")
    def test_model_inapplicable_exits_now(self):
        p = simple_pool(400)
        sc = {"healthy": True, "score_fraction": {}, "ranks": {}, "invalid_reasons": {"X0000.SH": [("F0", "MODEL_INAPPLICABLE")]}}
        c, act = update_missing_counters({}, ["X0000.SH"], True, sc, p)
        self.assertEqual(act["X0000.SH"], "EXIT_MODEL")
        self.assertEqual(c, {})

    @case("G05-14")
    def test_package_ratio_95(self):
        def weeks(bad_n):
            w = ["HEALTHY"] * 100
            for k in range(bad_n):
                w[10 * (k + 1)] = "BAD"
            return w
        r95, r94 = package_availability(weeks(5)), package_availability(weeks(6))
        self.assertEqual((r95["healthy_weeks"], r95["status"]), (95, "PASS"))
        self.assertEqual((r94["healthy_weeks"], r94["status"]), (94, "FAIL"))
        self.assertIn("HEALTHY_RATIO_BELOW_95", r94["failures"])

    @case("G05-15")
    def test_streak_failure(self):
        r = package_availability(["HEALTHY"] * 97 + ["BAD"] * 3)
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("BAD_COVERAGE_STREAK_3", r["failures"])
        self.assertEqual(r["healthy_ratio"], Fraction(97, 100))

    @case("G05-16")
    def test_unassessed_kept_in_denominator(self):
        r = package_availability(["HEALTHY"] * 97 + ["UNASSESSED"] * 3)
        self.assertEqual((r["status"], r["assessed_weeks"], r["unassessed_weeks"]), ("INSUFFICIENT_EVIDENCE", 100, 3))
        r2 = package_availability(["HEALTHY"] * 90 + ["UNASSESSED"] * 10)
        self.assertEqual(r2["status"], "INSUFFICIENT_EVIDENCE")
        r3 = package_availability(["HEALTHY"] * 80 + ["BAD"] * 10 + ["UNASSESSED"] * 10)
        self.assertEqual(r3["status"], "FAIL")

    @case("G05-17")
    def test_metamorphic_monotone(self):
        rng = random.Random(17)
        for _ in range(200):
            n = rng.randint(2, 30)
            vals = {f"s{i}": Decimal(rng.randint(-50, 50)) for i in range(n)}
            base = percentile_scores(vals, "HIGHER_BETTER")
            a, b = Decimal(rng.randint(1, 9)), Decimal(rng.randint(-100, 100))
            tr = {k: a * v * v * v + b for k, v in vals.items()}          # 严格单调变换
            items = list(tr.items())
            rng.shuffle(items)
            self.assertEqual(percentile_scores(dict(items), "HIGHER_BETTER"), base)

    @case("G05-18")
    def test_package_change_keeps_backbone(self):
        p = simple_pool(400)
        ids = p["security_ids"]
        vals = fv({s: str(i) for i, s in enumerate(ids)})
        p1, p2 = pkg("HIGHER_BETTER"), pkg("HIGHER_BETTER")
        p2["package_id"] = "OTHER_TEST_PKG"
        s1 = score_package(p1, p, {"F0": vals}, p["codes"])
        s2 = score_package(p2, p, {"F0": vals}, p["codes"])
        kw = dict(role="B1", plan_id="p", cutoff="c", e_decision=Fraction(100000), holdings=[], open_buy_sids=set(),
                  pool=p, counters={}, capital_revision=1, execute_session="x")
        a, _ = plan_week(score=s1, **kw)
        b, _ = plan_week(score=s2, **kw)
        self.assertEqual(a["buy_candidates_ordered"], b["buy_candidates_ordered"])
        self.assertEqual({k: v for k, v in a.items() if k != "package_id"}, {k: v for k, v in b.items() if k != "package_id"})
        self.assertEqual(C.CENTER, Fraction(1, 50))


if __name__ == "__main__":
    unittest.main()
