import copy
import re
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from smallcap import contracts as C
from smallcap.bundle import (check_baseline, check_exclusive_scopes, check_rulebook, fee_coverage, load_bundle)
from smallcap.contracts import ContractError
from smallcap.factors import compute_factor, score_package
from smallcap.fees import FeeSchedule
from smallcap.forward import dispatch_mode, forward_preconditions
from smallcap.rules import RuleBook
from tests.acceptance import case
from tests.fixtures import (ENGINEERING_BUNDLE, EXTERNAL_TEMPLATE, ROOT, EventFactory, fee_rule, new_ledger,
                            test_fee_payload, test_rulebook_payload, trade_rule)


class G01(unittest.TestCase):
    @case("G01-01")
    def test_constants_match_v5(self):
        b = load_bundle(copy.deepcopy(ENGINEERING_BUNDLE))
        self.assertEqual(b.raw["baseline"], C.BASELINE)
        self.assertEqual(C.STOCK_TARGET, Fraction(3, 5))
        self.assertEqual(C.CENTER * C.SLOTS, C.STOCK_TARGET)            # 60%/30=2%，无98%/90%
        self.assertEqual((C.POOL_LIMIT, C.SLOTS, C.RETAIN_RANK), (400, 30, 45))
        self.assertEqual((C.DD_WARNING, C.DD_RETIRE, C.DD_LIMIT), (Fraction(1, 5), Fraction(1, 4), Fraction(3, 10)))
        self.assertNotIn("0.98", C.BASELINE.values())
        self.assertNotIn("0.90", C.BASELINE.values())

    @case("G01-02")
    def test_baseline_mismatch(self):
        b = copy.deepcopy(ENGINEERING_BUNDLE)
        b["baseline"]["stock_target"] = "0.70"
        with self.assertRaises(ContractError) as cm:
            load_bundle(b)
        self.assertEqual(cm.exception.code, "BASELINE_MISMATCH")

    @case("G01-03")
    def test_scope_overlap_rejected(self):
        r = check_exclusive_scopes({"X0001.SH", "E510300.SH"}, {"X0001.SH", "X0002.SH"})
        self.assertEqual(r.status, C.INVALID)
        self.assertEqual(r.codes(), ["SCOPE_OVERLAP"])
        self.assertTrue(check_exclusive_scopes({"E510300.SH"}, {"X0001.SH"}).ok)

    @case("G01-04")
    def test_test_package_engineering_only(self):
        from smallcap.evaluation import qualify_config
        cells = [{"start": s, "scenario": sc, "startup_status": "STARTED", "retirement_triggered": False,
                  "MDD_exact": {"num": "1", "den": "10"}, "mdd_over_30": False, "CAGR_decimal": "0.05",
                  "package_availability": "PASS", "evidence_reasons": [], "annual_fee_rate": "0.001"}
                 for s in ("A0", "A1", "A2", "A3") for sc in ("S0", "S1", "S2", "S3")]
        q = qualify_config(cells, test_package=True, external_verified={"x": True}, benchmark_specified=True)
        self.assertEqual(q["status"], "ENGINEERING_ONLY")
        pkg = ENGINEERING_BUNDLE["profiles"]["factor"]["payload"]
        self.assertEqual(pkg["purpose"], "TEST_ONLY")

    @case("G01-05")
    def test_missing_capital_and_permission_blocks_plan(self):
        b = load_bundle(copy.deepcopy(EXTERNAL_TEMPLATE))
        codes = {r.code for r in forward_preconditions(b)}
        self.assertIn("ACTUAL_CAPITAL_NOT_PROVIDED", codes)
        self.assertIn("PERMISSIONS_NOT_PROVIDED", codes)
        from smallcap.cli import main
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["plan", "--bundle", str(ROOT / "handoff/小市值周频策略_V5D2/inputs/external_inputs.template.json")])
        self.assertEqual(rc, 3)
        self.assertIn("BLOCKED", buf.getvalue())
        self.assertNotIn("500000", buf.getvalue().split('"blocks"')[0])

    @case("G01-06")
    def test_future_available_at_rejected(self):
        spec = ENGINEERING_BUNDLE["profiles"]["factor"]["payload"]["factors"][0]
        cutoff = datetime(2025, 12, 31, 12, tzinfo=timezone.utc)
        future = datetime(2026, 4, 17, 1, tzinfo=timezone.utc)
        facts = {"A": {"fixture_value": ("1.5", "VALID", future)}, "B": {"fixture_value": ("2.0", "VALID", cutoff)}}
        out = compute_factor(spec, ["A", "B"], cutoff, facts)
        self.assertEqual(out["A"].status, "SOURCE_UNKNOWN")
        self.assertEqual(out["A"].reason, "FUTURE_DEPENDENCY")
        self.assertEqual(out["B"].status, "VALID")

    @case("G01-07")
    def test_type_errors(self):
        for bad in (float("nan"), 1.0, True, "NaN", "Infinity", "1e5", "01.0", Decimal("NaN")):
            with self.assertRaises(ContractError):
                C.money(bad)
        for bad in (True, 1.5, "100", -1):
            with self.assertRaises(ContractError):
                C.shares(bad)
        self.assertEqual(C.shares(100), 100)

    @case("G01-08")
    def test_rule_overlap_ambiguous(self):
        payload = test_rulebook_payload(extra=[trade_rule("DUP", start="2020-01-01", end="2021-01-01")])
        with self.assertRaises(ContractError) as cm:
            check_rulebook(payload)
        self.assertEqual(cm.exception.code, "RULE_AMBIGUOUS")
        with self.assertRaises(ContractError) as cm:
            RuleBook(payload).lookup("SH", "MAIN_SH", date(2020, 6, 1))
        self.assertEqual(cm.exception.code, "RULE_AMBIGUOUS")

    @case("G01-09")
    def test_fee_gap_insufficient(self):
        payload = test_fee_payload()
        payload["fee_rules"] = [r for r in payload["fee_rules"] if r["fee_kind"] != "STAMP"] + [
            fee_rule("STAMP", "0.001", side="SELL", start="2016-01-01", end="2100-01-01"),
            fee_rule("STAMP", "0", side="BUY", start="2000-01-01", end="2100-01-01", fid="sb")]
        r = fee_coverage(payload, date(2011, 1, 7), date(2026, 10, 16))
        self.assertEqual(r.status, C.UNKNOWN)
        self.assertTrue(any(x.code == "FEE_SCHEDULE_INCOMPLETE" and "STAMP" in x.scope_id for x in r.reasons))
        fs = FeeSchedule(payload)
        # 空档期没有“今天费率/记0”分支：印花税规则在该日不存在 → FEE_SCHEDULE_INCOMPLETE
        with self.assertRaises(ContractError) as cm:
            fs.order_fees("SH", "SELL", date(2012, 1, 5), Decimal(10000), 1000)
        self.assertEqual(cm.exception.code, "FEE_SCHEDULE_INCOMPLETE")
        self.assertEqual(fs.order_fees("SH", "SELL", date(2017, 1, 5), Decimal(10000), 1000)["STAMP"], Decimal("10.00"))
        no_comm = test_fee_payload()
        no_comm["fee_rules"] = [r for r in no_comm["fee_rules"] if r["fee_kind"] != "COMMISSION"] + [
            fee_rule("COMMISSION", "0.00025", "5", start="2016-01-01")]
        with self.assertRaises(ContractError) as cm:
            FeeSchedule(no_comm).order_fees("SH", "BUY", date(2012, 1, 5), Decimal(1000), 100)
        self.assertEqual(cm.exception.code, "FEE_SCHEDULE_INCOMPLETE")

    @case("G01-10")
    def test_live_order_refused(self):
        self.assertEqual(dispatch_mode("LIVE_ORDER").status, C.INVALID)
        self.assertEqual(dispatch_mode("LIVE_ORDER").codes(), ["LIVE_ORDER_NOT_AUTHORIZED"])
        from smallcap.cli import main
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["send-live-orders"])
        src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "smallcap").glob("*.py"))
        for forbidden in ("xtquant", "order_stock", "cancel_order_stock", "XtQuantTrader", "requests.post"):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
