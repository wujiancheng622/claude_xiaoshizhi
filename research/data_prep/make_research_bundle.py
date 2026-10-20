"""生成研究用 Bundle（临时 Tushare 来源；所有外部规则/费税档案均为 DECLARED，未经用户核验，不得当作 VERIFIED）。

规则与费税取自公开政策（2026-10-19 依常识整理，需用户按 G18-09/G18-11 核对后才能升级）：
- 主板涨跌幅 10%；创业板 2020-08-24 起 20%（此前 10%、风险警示 5%）；科创板 20%、单笔≥200 股；风险警示股 5%（2026-07-06 起 10%，沿用 2026-10-15 前期研究口径）；退市整理期按 10%（首日不设限未建模）。
- 主板市值退市红线：2020-12-31 起 3 亿元；2024-10-30 起 5 亿元（单日检查用于池资格与持仓风险）。
- 研究佣金 0.025%、单笔最低 5 元（V5D2 基线）；印花税卖出 0.1%（2023-08-28 起 0.05%）；
  过户费：沪市 2015-08-01 前按股数 0.0006 元/股（面值 0.6‰）且单笔最低 1 元，深市此前不收；
  2015-08-01 起沪深成交额 0.02‰，2022-04-29 起 0.01‰。
- 股息红利差别化个税（按股权登记日选择）：2013-01-01 前 10%（全额代扣）；2013-01-01 至 2015-09-07：
  ≤1 个月 20%、≤1 年 10%、>1 年 5%（派发时先扣 5%）；2015-09-08 起 ≤1 个月 20%、≤1 年 10%、>1 年 0（派发时不扣）。
用法：python research/data_prep/make_research_bundle.py  → profiles/research_bundles/*.json
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
from smallcap.contracts import BASELINE, EXECUTION_TECHNICAL  # noqa: E402

OUT = ROOT / "profiles" / "research_bundles"
PKG = ROOT / "profiles" / "factor_packages"
DESC = ("2026-10-19 研究用（临时 Tushare 研究快照）。公开政策整理，未经用户核验；不得升级为 VERIFIED_FOR_SCOPE。")


def scope():
    return {"purpose": "HISTORICAL_RESEARCH", "start_date": "2009-01-01", "end_date": "2026-10-14",
            "markets": ["SH", "SZ"], "account_ref": None, "description": DESC}


def rule(rid, market, board, scope_ref, start, end, public, pct, floor, grid=None):
    return {"rule_id": rid, "market": market, "board": board, "security_scope_ref": scope_ref,
            "effective_from": start, "effective_to": end, "public_at": public + "T00:00:00+08:00",
            "quantity_grid": grid or {"buy_min": 100, "buy_step": 100, "sell_min": 100, "sell_step": 100,
                                      "allow_full_odd_exit": True, "max_order_quantity": 1000000},
            "price_rule": {"tick": "0.01", "method": "PERCENT_REFERENCE", "reference_field": "reference_preclose",
                           "up_percent": pct, "down_percent": pct, "bound_data_ref": None, "bound_rounding": "HALF_UP_TICK",
                           "intraday_band_id": None},
            "settlement_id": "CN-A-T1", "marketcap_floor": floor,
            "marketcap_floor_status": "NOT_APPLICABLE" if floor is None else "VALUE",
            "evidence_ref": "PUBLIC_POLICY_DECLARED_UNVERIFIED"}


def rulebook():
    rules = []
    periods = [("2009-01-01", "2020-12-31", "2009-01-01", None),
               ("2020-12-31", "2024-10-30", "2020-12-31", "300000000"),
               ("2024-10-30", "2026-07-06", "2024-04-30", "500000000"),
               ("2026-07-06", "2027-01-01", "2026-06-01", "500000000")]
    for market, board in (("SH", "MAIN_SH"), ("SZ", "MAIN_SZ")):
        for k, (a, b, pub, floor) in enumerate(periods):
            rules.append(rule(f"{board}-NORMAL-{k}", market, board, "NORMAL", a, b, pub, "0.10", floor))
            rules.append(rule(f"{board}-RW-{k}", market, board, "RISK_WARNING", a, b, pub,
                              "0.10" if a >= "2026-07-06" else "0.05", floor))
            rules.append(rule(f"{board}-DA-{k}", market, board, "DELISTING_ARRANGEMENT", a, b, pub, "0.10", floor))
    # 创业板：2020-08-24 注册制改革起涨跌幅 20%（含风险警示），此前 10%（风险警示 5%）；2020-12-31 起市值红线 3 亿
    cg = {"buy_min": 100, "buy_step": 100, "sell_min": 100, "sell_step": 100, "allow_full_odd_exit": True,
          "max_order_quantity": 300000}
    for k, (a, b, pub, n, rw, floor) in enumerate([("2009-10-30", "2020-08-24", "2009-10-30", "0.10", "0.05", None),
                                                   ("2020-08-24", "2020-12-31", "2020-06-12", "0.20", "0.20", None),
                                                   ("2020-12-31", "2027-01-01", "2020-12-31", "0.20", "0.20", "300000000")]):
        for sc, pct in (("NORMAL", n), ("RISK_WARNING", rw), ("DELISTING_ARRANGEMENT", n)):
            rules.append(rule(f"CHINEXT-{sc}-{k}", "SZ", "CHINEXT", sc, a, b, pub, pct, floor, cg))
    # 科创板：2019-07-22 起涨跌幅 20%；单笔 ≥200 股、超出部分 1 股递增，余额不足 200 股须一次卖出；市值红线 3 亿
    sg = {"buy_min": 200, "buy_step": 1, "sell_min": 200, "sell_step": 1, "allow_full_odd_exit": True,
          "max_order_quantity": 100000}
    for sc in ("NORMAL", "RISK_WARNING", "DELISTING_ARRANGEMENT"):
        rules.append(rule(f"STAR-{sc}", "SH", "STAR", sc, "2019-07-22", "2027-01-01", "2019-03-01", "0.20", "300000000", sg))
    return {"rulebook_id": "CN-A-ALLBOARDS-RESEARCH-20261019", "rules": rules,
            "settlements": [{"settlement_id": "CN-A-T1", "cash_core_mode": "TRADE_CASH_BOOK",
                             "buy_sellable_delay_sessions": 1, "buy_sellable_release_time": "09:15:00",
                             "sell_proceeds_trade_delay_sessions": 0, "sell_proceeds_withdraw_delay_sessions": 1,
                             "sell_proceeds_release_time": "15:00:00", "tax_acquisition_date_field": "TRADE_DATE",
                             "tax_delivery_date_field": "TRADE_DATE", "fee_settlement_timing": "WITH_TRADE",
                             "evidence_ref": "PUBLIC_POLICY_DECLARED_UNVERIFIED"}]}


def fee(fid, kind, start, end, market, side, base, rate, minimum="0"):
    return {"fee_id": fid, "fee_kind": kind, "effective_from": start, "effective_to": end, "market": market,
            "side": side, "base_kind": base, "rate": rate, "minimum": minimum, "maximum": None,
            "included_in_commission": False, "charge_unit": "ORDER_CUMULATIVE", "round_quantum": "0.01",
            "round_mode": "HALF_UP", "settlement_id": "CN-A-T1", "evidence_ref": "PUBLIC_POLICY_DECLARED_UNVERIFIED"}


def tax(rid, start, end, dtype, wh, tiers, base):
    return {"rule_id": rid, "effective_from": start, "effective_to": end, "security_class": "INDIVIDUAL_A_SHARE",
            "dividend_type": dtype, "initial_withholding_rate": wh,
            "tiers": [{"max_calendar_months_inclusive": m, "rate": r} for m, r in tiers],
            "tax_base_definition": base, "acquisition_inheritance_rule": "INHERIT_PARENT", "rounding_unit": "ENTITLEMENT",
            "evidence_ref": "财税[2005]102号/[2012]85号/[2015]101号（公开政策整理，未核验）",
            "selection_date_basis": "EQUITY_RECORD_DATE"}


def fees():
    rules = [fee("COMM", "COMMISSION", "2000-01-01", "2100-01-01", "BOTH", "BOTH", "NOTIONAL", "0.00025", "5"),
             fee("STAMP-SELL-1", "STAMP", "2008-09-19", "2023-08-28", "BOTH", "SELL", "NOTIONAL", "0.001"),
             fee("STAMP-SELL-2", "STAMP", "2023-08-28", "2100-01-01", "BOTH", "SELL", "NOTIONAL", "0.0005"),
             fee("STAMP-BUY", "STAMP", "2008-09-19", "2100-01-01", "BOTH", "BUY", "NOTIONAL", "0"),
             fee("TRANSFER-SH-1", "TRANSFER", "2000-01-01", "2015-08-01", "SH", "BOTH", "SHARES", "0.0006", "1"),
             fee("TRANSFER-SZ-1", "TRANSFER", "2000-01-01", "2015-08-01", "SZ", "BOTH", "NOTIONAL", "0"),
             fee("TRANSFER-2", "TRANSFER", "2015-08-01", "2022-04-29", "BOTH", "BOTH", "NOTIONAL", "0.00002"),
             fee("TRANSFER-3", "TRANSFER", "2022-04-29", "2100-01-01", "BOTH", "BOTH", "NOTIONAL", "0.00001")]
    taxes = []
    for dtype, base in (("CASH", "每股税前现金红利"), ("STOCK", "每股送红股面值（1 元/股×送股比例）")):
        taxes += [tax(f"T2005-{dtype}", "2005-06-13", "2013-01-01", dtype, "0.10", [(None, "0.10")], base),
                  tax(f"T2013-{dtype}", "2013-01-01", "2015-09-08", dtype, "0.05",
                      [(1, "0.20"), (12, "0.10"), (None, "0.05")], base),
                  tax(f"T2015-{dtype}", "2015-09-08", "2100-01-01", dtype, "0",
                      [(1, "0.20"), (12, "0.10"), (None, "0")], base)]
    return {"fee_profile_id": "fees-cn-research-20261019", "purpose": "RESEARCH", "fee_rules": rules,
            "dividend_tax_rules": taxes, "research_commission_rate": "0.00025", "research_minimum_commission": "5",
            "broker_contract_ref": None}


def bundle(pkg_file: Path) -> dict:
    prof = json.loads(pkg_file.read_text(encoding="utf-8"))
    pid = prof["payload"]["package_id"]
    p = {
        "factor": prof,
        "universe": {"profile_id": "universe-cn-main-research", "status": "DECLARED", "scope": scope(),
                     "source_refs": ["TUSHARE_STOCK_BASIC", "TUSHARE_NAMECHANGE", "TUSHARE_SW2021_MEMBERS"], "payload": {
                         "universe_id": "cn-a-allboards-research", "stock_markets": ["SH", "SZ"], "exclude_financial": True,
                         "etf_security_ids": [], "stock_scope_ref": "沪深主板（含原中小板）、创业板、科创板普通A股",
                         "identity_history_ref": "TUSHARE_STOCK_BASIC_INCL_DELISTED",
                         "industry_history_ref": "TUSHARE_SW2021_L1_APPROX_HISTORY",
                         "permissions": [{"board": "MAIN_SH", "status": "ALLOWED", "evidence_ref": "用户 2026-10-19 确认"},
                                         {"board": "MAIN_SZ", "status": "ALLOWED", "evidence_ref": "用户 2026-10-19 确认"},
                                         {"board": "CHINEXT", "status": "ALLOWED", "evidence_ref": "用户 2026-10-19 确认有权限"},
                                         {"board": "STAR", "status": "ALLOWED", "evidence_ref": "用户 2026-10-19 确认有权限"}],
                         "mutual_exclusivity_checked": True}},
        "rules": {"profile_id": "rules-cn-main-research", "status": "DECLARED", "scope": scope(),
                  "source_refs": ["PUBLIC_POLICY"], "payload": rulebook()},
        "fees": {"profile_id": "fees-cn-research-20261019", "status": "DECLARED", "scope": scope(),
                 "source_refs": ["PUBLIC_POLICY"], "payload": fees()},
        "sources": {"profile_id": "sources-tushare-temp-research", "status": "DECLARED", "scope": scope(),
                    "source_refs": ["TUSHARE_PROXY_20261015_20261019"], "payload": {
                        "source_profile_id": "sources-tushare-temp-research", "priority": ["QMT", "TUSHARE"],
                        "qmt_maintenance_research": True, "account_source": "BROKER_CONFIRMED_ONLY",
                        "mappings": [{"canonical_field": "raw_close", "provider": "TUSHARE", "endpoint": "daily",
                                      "source_field": "close", "source_unit": "CNY", "canonical_unit": "CNY",
                                      "multiply": "1", "basis_id": "RAW", "historical_semantics": "HISTORICAL",
                                      "scope_ref": "TUSHARE_TEMP_RESEARCH", "evidence_ref": "D03.4"}],
                        "substitution_policy": "EXPLICIT_SAME_BASIS_ONLY"}},
        "bootstrap": {"profile_id": "bootstrap-replay-all-cash", "status": "DECLARED", "scope": scope(),
                      "source_refs": ["V5D2_D16"], "payload": {
                          "bootstrap_id": "replay-all-cash", "mode": "REPLAY_ALL_CASH", "cash_amount": None,
                          "positions_ref": None, "cash_ownership_ref": None, "broker_snapshot_ref": None,
                          "open_orders_ref": None, "tax_lots_ref": None, "corporate_entitlements_ref": None,
                          "previous_state_ref": None, "retirement_history_checked": True}},
        "benchmark": {"profile_id": "benchmark-csi2000-etf-563300", "status": "DECLARED", "scope": scope(),
                      "source_refs": ["TUSHARE_FUND_BASIC", "TUSHARE_FUND_DAILY"], "payload": {
                          "benchmark_id": "ETF-563300-CSI2000",
                          "selection_reason": "替代方案：不自建、直接买小微盘宽基ETF。选中证2000（风格最接近微盘400池的可交易宽基），"
                                              "563300 规模与成交最活跃；159907 广发国证2000（2011-08 上市）作长窗口补充对照。用户可改。",
                          "selected_before_results_at": "2026-10-19T20:30:00+08:00",
                          "securities": ["563300.SH"],
                          "cash_policy_ref": "同仓位对照：60% ETF + 40% 现金（与策略股票目标一致），另报 100% ETF",
                          "fee_profile_ref": "fees-cn-research-20261019（ETF 免印花税；管理费0.50%/托管0.10%已在净值中）",
                          "listing_history_ref": "TUSHARE_FUND_BASIC list_date=2023-09-14",
                          "price_basis_ref": "fund_daily × fund_adj 复权总收益",
                          "window_policy": "COMMON_ACTUAL_LIFETIME"}},
    }
    return {"bundle_version": "V5D2", "baseline": copy.deepcopy(BASELINE),
            "execution_technical": copy.deepcopy(EXECUTION_TECHNICAL),
            "study": {"study_id": f"tushare-research-20261019-{pid}", "detail_version": "V5D2", "end_date": "2026-10-14",
                      "capital_mode": "DEFAULT_TWO", "capitals": ["100000", "500000"],
                      "start_years": [2011, 2016, 2021, 2024], "roles": ["B0", "B1", "B2"],
                      "scenarios": ["S0", "S1", "S2", "S3"], "execution_mode": "REPLAY_DAILY_PROXY",
                      "fact_snapshot_id": "TUSHARE_STORE_20261019", "purpose": "RESEARCH"},
            "profiles": p}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in sorted(PKG.glob("*.json")):
        b = bundle(f)
        out = OUT / f"tushare_research_{json.loads(f.read_text())['payload']['package_id']}.json"
        out.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
        print(out)


if __name__ == "__main__":
    main()
