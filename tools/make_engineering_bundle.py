"""生成 TEST_ONLY 工程联调 Bundle（合成市场专用）。所有 Profile 均为 DECLARED + TEST_ONLY，不是任何真实事实。

用法：python3 tools/make_engineering_bundle.py [--mode REPLAY_DAILY_PROXY|REPLAY_MINUTE_WINDOW] [--out examples/...json]
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fixtures import ENGINEERING_BUNDLE, stub_tax_rules, test_fee_payload, test_rulebook_payload  # noqa: E402

DESC = "TEST_ONLY 合成市场工程联调；不是真实规则/费率/权限/数据能力，不得升级为 VERIFIED。"


def scope():
    return {"purpose": "TEST_ONLY", "start_date": None, "end_date": None, "markets": ["SH", "SZ"], "account_ref": None,
            "description": DESC}


def build(mode: str) -> dict:
    b = copy.deepcopy(ENGINEERING_BUNDLE)
    b["study"].update(study_id=f"engineering-synthetic-TEST_ONLY-{mode}", end_date="2026-10-16",
                      execution_mode=mode, fact_snapshot_id="TEST_ONLY_SYNTHETIC_SEED7", purpose="ENGINEERING_ONLY")
    p = b["profiles"]
    p["universe"] = {"profile_id": "universe-test-synthetic", "status": "DECLARED", "scope": scope(),
                     "source_refs": ["TEST_ONLY_SYNTHETIC"], "payload": {
                         "universe_id": "synthetic-universe", "stock_markets": ["SH", "SZ"], "exclude_financial": True,
                         "etf_security_ids": ["E510000.SH"], "stock_scope_ref": "TEST_ONLY_SYNTHETIC",
                         "identity_history_ref": "TEST_ONLY_SYNTHETIC", "industry_history_ref": "TEST_ONLY_SYNTHETIC",
                         "permissions": [{"board": "MAIN_SH", "status": "ALLOWED", "evidence_ref": "TEST_ONLY"},
                                         {"board": "MAIN_SZ", "status": "ALLOWED", "evidence_ref": "TEST_ONLY"},
                                         {"board": "CHINEXT", "status": "UNKNOWN", "evidence_ref": None},
                                         {"board": "STAR", "status": "UNKNOWN", "evidence_ref": None}],
                         "mutual_exclusivity_checked": True}}
    p["rules"] = {"profile_id": "rules-test-synthetic", "status": "DECLARED", "scope": scope(),
                  "source_refs": ["TEST_ONLY_SYNTHETIC"], "payload": test_rulebook_payload()}
    fees = test_fee_payload(stamp_rate="0.001", transfer_rate="0.00002", tax_rules=stub_tax_rules())
    fees["fee_profile_id"] = "fees-test-synthetic"
    p["fees"] = {"profile_id": "fees-test-synthetic", "status": "DECLARED", "scope": scope(),
                 "source_refs": ["TEST_ONLY_SYNTHETIC"], "payload": fees}
    p["sources"] = {"profile_id": "sources-test-synthetic", "status": "DECLARED", "scope": scope(),
                    "source_refs": ["TEST_ONLY_SYNTHETIC"], "payload": {
                        "source_profile_id": "sources-test-synthetic", "priority": ["QMT", "TUSHARE"],
                        "qmt_maintenance_research": True, "account_source": "BROKER_CONFIRMED_ONLY",
                        "mappings": [{"canonical_field": "raw_close", "provider": "TEST", "endpoint": "synthetic",
                                      "source_field": "close", "source_unit": "CNY", "canonical_unit": "CNY",
                                      "multiply": "1", "basis_id": "RAW", "historical_semantics": "HISTORICAL",
                                      "scope_ref": "TEST_ONLY_SYNTHETIC", "evidence_ref": "TEST_ONLY"}],
                        "substitution_policy": "EXPLICIT_SAME_BASIS_ONLY"}}
    p["bootstrap"] = {"profile_id": "bootstrap-test-replay", "status": "DECLARED", "scope": scope(),
                      "source_refs": ["TEST_ONLY_SYNTHETIC"], "payload": {
                          "bootstrap_id": "replay-all-cash", "mode": "REPLAY_ALL_CASH", "cash_amount": None,
                          "positions_ref": None, "cash_ownership_ref": None, "broker_snapshot_ref": None,
                          "open_orders_ref": None, "tax_lots_ref": None, "corporate_entitlements_ref": None,
                          "previous_state_ref": None, "retirement_history_checked": True}}
    # benchmark 保持 NOT_PROVIDED：外部 ETF 对象须由用户在看结果前指定
    return b


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="REPLAY_DAILY_PROXY")
    ap.add_argument("--out", default=str(ROOT / "examples" / "engineering_synthetic_bundle.json"))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(a.mode), ensure_ascii=False, indent=1), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
