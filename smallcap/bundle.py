"""唯一外部输入 Bundle 的加载、结构/业务校验与 StudyDefinition 派生（H02、H09、D15.3）。"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .contracts import (BASELINE, DETAIL_VERSION, EXECUTION_MODES, EXECUTION_TECHNICAL, ContractError,
                        Result, OK, UNKNOWN, INVALID, money, parse_date, reason)

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
STUDY_FIELDS = frozenset({"study_id", "detail_version", "end_date", "capital_mode", "capitals", "start_years",
                          "roles", "scenarios", "execution_mode", "fact_snapshot_id", "purpose"})
PROFILE_NAMES = ("factor", "universe", "rules", "fees", "sources", "bootstrap", "benchmark")


def _schema_validator(name: str = "bundle.schema.json"):
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def schema_errors(bundle: dict) -> list[str]:
    v = _schema_validator()
    return sorted(f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(bundle))


def check_baseline(bundle: dict) -> None:
    """固定常量副本必须与 V5D2 基线逐项相等；不接受命令行临时修改。"""
    if bundle.get("bundle_version") != DETAIL_VERSION:
        raise ContractError("VERSION_MISMATCH", str(bundle.get("bundle_version")))
    b = bundle.get("baseline", {})
    if set(b) != set(BASELINE):
        raise ContractError("BASELINE_MISMATCH", "keys differ")
    for k, v in BASELINE.items():
        if b[k] != v or type(b[k]) is not type(v):
            raise ContractError("BASELINE_MISMATCH", f"{k}={b[k]!r} != {v!r}")
    if bundle.get("execution_technical") != EXECUTION_TECHNICAL:
        raise ContractError("EXECUTION_PROFILE_MISMATCH")


def normalize_study(bundle: dict) -> dict:
    """纯函数：由已校验的 Bundle 一次派生内部 StudyDefinition（H02.1 映射表）。"""
    s = bundle["study"]
    if set(s) != STUDY_FIELDS:
        raise ContractError("STUDY_FIELDS", ",".join(sorted(set(s) ^ STUDY_FIELDS)))
    if s["execution_mode"] not in EXECUTION_MODES:
        raise ContractError("MODE_INVALID", str(s["execution_mode"]))
    ps = bundle["profiles"]
    out = copy.deepcopy(s)
    for name, key in (("sources", "source_profile_id"), ("universe", "universe_profile_id"),
                      ("rules", "rulebook_profile_id"), ("fees", "fee_profile_id"),
                      ("factor", "factor_profile_id"), ("bootstrap", "bootstrap_profile_id"),
                      ("benchmark", "benchmark_profile_id")):
        out[key] = ps[name]["profile_id"]
    for name, key in (("sources", "source_profile_id"), ("fees", "fee_profile_id")):
        payload = ps[name]["payload"]
        if payload is not None and payload.get(key) != ps[name]["profile_id"]:
            raise ContractError("PROFILE_REFERENCE_MISMATCH", f"{name}.payload.{key}")
    for name, inner, outer in (("rules", "rulebook_id", "rulebook_id"), ("factor", "package_id", "factor_package_id"),
                               ("bootstrap", "bootstrap_id", "bootstrap_id")):
        payload = ps[name]["payload"]
        out[outer] = payload[inner] if payload is not None else None
    out["benchmark_definition"] = copy.deepcopy(ps["benchmark"]["payload"])
    out["benchmark_status"] = ps["benchmark"]["status"]
    out["baseline"] = bundle["baseline"]          # 只读引用，不复制修改
    out["execution_technical"] = bundle["execution_technical"]
    return out


@dataclass
class LoadedBundle:
    raw: dict
    study: dict
    findings: list            # 业务层原因（非致命的外部不足）

    def profile(self, name: str) -> dict:
        return self.raw["profiles"][name]

    def payload(self, name: str):
        return self.raw["profiles"][name]["payload"]

    def status(self, name: str) -> str:
        return self.raw["profiles"][name]["status"]


def load_bundle(src: str | Path | dict) -> LoadedBundle:
    """结构 → 版本/常量 → profile状态及scope → 日期区间唯一匹配（H09.1）。"""
    bundle = json.loads(Path(src).read_text(encoding="utf-8")) if not isinstance(src, dict) else src
    if not isinstance(bundle, dict):
        raise ContractError("SCHEMA_INVALID", "bundle must be an object")
    check_baseline(bundle)          # 固定常量先于结构校验：改动60%等直接报 BASELINE_MISMATCH
    errs = schema_errors(bundle)
    if errs:
        raise ContractError("SCHEMA_INVALID", "; ".join(errs[:5]))
    study = normalize_study(bundle)
    findings = []
    findings += check_profiles(bundle)
    rules = bundle["profiles"]["rules"]["payload"]
    if rules is not None:
        findings += check_rulebook(rules)
    fees = bundle["profiles"]["fees"]["payload"]
    if fees is not None:
        findings += check_fee_rules(fees)
        if rules is not None:
            known = {s["settlement_id"] for s in rules["settlements"]}
            for r in fees["fee_rules"]:
                if r["settlement_id"] not in known:
                    raise ContractError("PROFILE_REFERENCE_MISMATCH", f"fee {r['fee_id']} settlement {r['settlement_id']}")
    uni = bundle["profiles"]["universe"]["payload"]
    if uni is not None:
        findings += check_universe(uni)
    return LoadedBundle(bundle, study, findings)


def check_profiles(bundle: dict) -> list:
    out = []
    for name in PROFILE_NAMES:
        p = bundle["profiles"][name]
        if p["status"] == "NOT_PROVIDED":
            out.append(reason("EXTERNAL_INPUT_NOT_PROVIDED", "PROFILE", name, p["profile_id"]))
        elif p["status"] in ("DECLARED", "SAMPLE_CHECKED"):
            out.append(reason("EXTERNAL_INPUT_NOT_VERIFIED", "PROFILE", name, p["status"]))
        if p["scope"]["purpose"] == "TEST_ONLY":
            out.append(reason("TEST_ONLY_PROFILE", "PROFILE", name, "测试夹具，不得升级为真实事实"))
    if bundle["study"]["end_date"] is None:
        out.append(reason("END_DATE_NOT_FIXED", "STUDY", bundle["study"]["study_id"], "未固定共同终点D"))
    return out


def check_universe(uni: dict) -> list:
    etf = set(uni["etf_security_ids"])
    out = []
    for p in uni["permissions"]:
        if p["status"] == "UNKNOWN":
            out.append(reason("PERMISSION_UNKNOWN", "BOARD", p["board"], "该板块不买"))
    if not uni["mutual_exclusivity_checked"]:
        out.append(reason("MUTUAL_EXCLUSIVITY_UNCHECKED", "UNIVERSE", uni["universe_id"]))
    return out


def check_exclusive_scopes(etf_ids, stock_ids) -> Result:
    """ETF与股票证券范围必须互斥；重叠则拒绝共享运行，不生成同股分摊。"""
    both = sorted(set(etf_ids) & set(stock_ids))
    if both:
        return Result(INVALID, None, [reason("SCOPE_OVERLAP", "SECURITY", s, "拒绝共享运行") for s in both])
    return Result(OK, None)


def _intervals_check(rows, key_fn, label: str) -> list:
    out = []
    groups: dict = {}
    for r in rows:
        f, t = parse_date(r["effective_from"]), parse_date(r["effective_to"])
        if f >= t:
            raise ContractError(f"{label}_INTERVAL_INVALID", r.get("rule_id") or r.get("fee_id"))
        groups.setdefault(key_fn(r), []).append((f, t, r))
    for k, items in groups.items():
        items.sort(key=lambda x: x[0])
        for (f1, t1, r1), (f2, t2, r2) in zip(items, items[1:]):
            if f2 < t1:
                raise ContractError("RULE_AMBIGUOUS", f"{label} {k}: overlap {f2}")
            if f2 > t1:
                out.append(reason(f"{label}_GAP", label, str(k), f"{t1}..{f2}"))
    return out


def check_rulebook(rb: dict) -> list:
    from .rules import RuleBook
    RuleBook(rb)                               # 清算映射/价格方法等未识别取值 → RULE_UNSUPPORTED
    settlement_ids = {s["settlement_id"] for s in rb["settlements"]}
    for r in rb["rules"]:
        if r["settlement_id"] not in settlement_ids:
            raise ContractError("PROFILE_REFERENCE_MISMATCH", f"rule {r['rule_id']} settlement")
        if (r["marketcap_floor"] is None) != (r["marketcap_floor_status"] == "NOT_APPLICABLE"):
            raise ContractError("RULE_INVALID", f"marketcap floor fields {r['rule_id']}")
    # 同一市场/板块/日期存在多套规则时，各行必须使用可按风险状态选择的范围名
    from .rules import RULE_SCOPES
    by_board: dict = {}
    for r in rb["rules"]:
        by_board.setdefault((r["market"], r["board"]), []).append(r)
    for (m, b), rows in by_board.items():
        for r1 in rows:
            for r2 in rows:
                if r1 is r2 or r1["security_scope_ref"] == r2["security_scope_ref"]:
                    continue
                if r1["effective_from"] < r2["effective_to"] and r2["effective_from"] < r1["effective_to"]:
                    for r in (r1, r2):
                        if r["security_scope_ref"] not in RULE_SCOPES:
                            raise ContractError("RULE_SCOPE_UNSUPPORTED",
                                                f"{r['rule_id']}: {r['security_scope_ref']} 不在 {RULE_SCOPES}")
    return _intervals_check(rb["rules"], lambda r: (r["market"], r["board"], r["security_scope_ref"]), "RULE")


def check_fee_rules(fees: dict) -> list:
    for r in fees["fee_rules"]:
        if r["fee_kind"] == "COMMISSION" and r["charge_unit"] != "ORDER_CUMULATIVE":
            raise ContractError("FEE_RULE_INVALID", "commission must be ORDER_CUMULATIVE")
    out = []
    # 同一费种、市场、方向的生效区间不得重叠
    def expand(r):
        markets = ["SH", "SZ"] if r["market"] == "BOTH" else [r["market"]]
        sides = ["BUY", "SELL"] if r["side"] == "BOTH" else [r["side"]]
        return [(r["fee_kind"], m, s) for m in markets for s in sides]
    rows = []
    for r in fees["fee_rules"]:
        for k in expand(r):
            rows.append(dict(r, _k=k))
    out += _intervals_check(rows, lambda r: r["_k"], "FEE")
    for r in fees["dividend_tax_rules"]:
        if r["selection_date_basis"] != "EQUITY_RECORD_DATE":
            raise ContractError("TAX_SELECTION_BASIS", r["rule_id"])
    return out


REQUIRED_FEE_KINDS = ("COMMISSION", "STAMP", "TRANSFER")


def fee_coverage(fees: dict | None, start: date, end: date, markets=("SH", "SZ")) -> Result:
    """回放区间[start,end]内必需费种是否连续有生效规则；缺口不得套今天费率（G01-09）。"""
    if fees is None:
        return Result(UNKNOWN, None, [reason("FEE_SCHEDULE_INCOMPLETE", "PROFILE", "fees", "未提供费用表")])
    gaps = []
    for kind in REQUIRED_FEE_KINDS:
        for m in markets:
            for side in ("BUY", "SELL"):
                iv = sorted((parse_date(r["effective_from"]), parse_date(r["effective_to"]))
                            for r in fees["fee_rules"] if r["fee_kind"] == kind and r["market"] in (m, "BOTH")
                            and r["side"] in (side, "BOTH"))
                cur = start
                for f, t in iv:
                    if f > cur:
                        break
                    if t > cur:
                        cur = t
                if cur <= end:
                    gaps.append(reason("FEE_SCHEDULE_INCOMPLETE", "FEE", f"{kind}/{m}/{side}", f"uncovered from {cur}"))
    return Result(OK if not gaps else UNKNOWN, None, gaps)


def rule_coverage(rb: dict | None, start: date, end: date, boards: list[tuple[str, str]]) -> Result:
    if rb is None:
        return Result(UNKNOWN, None, [reason("RULE_UNKNOWN", "PROFILE", "rules", "未提供RuleBook")])
    gaps = []
    for market, board in boards:
        iv = sorted((parse_date(r["effective_from"]), parse_date(r["effective_to"])) for r in rb["rules"]
                    if r["market"] == market and r["board"] == board)
        cur = start
        for f, t in iv:
            if f > cur:
                break
            cur = max(cur, t)
        if cur <= end:
            gaps.append(reason("RULE_UNKNOWN", "BOARD", f"{market}/{board}", f"uncovered from {cur}"))
    return Result(OK if not gaps else UNKNOWN, None, gaps)
