"""固定研究矩阵：B0/B1/B2 × 金额 × 四起点 × 四情景（默认96格），所有格都产记录（D16、H08.2）。

外部输入不足（终点D未固定、RuleBook/费用/因子包/市场数据缺失、费率区间空档等）时，单元记录为
NOT_STARTED_UNKNOWN 与原因，不删格、不改参数、不切来源。
"""
from __future__ import annotations

import json
import multiprocessing as mp
from datetime import date
from pathlib import Path

from .bundle import LoadedBundle, check_exclusive_scopes, fee_coverage, rule_coverage
from .contracts import ROLES, SCENARIOS, START_IDS, ContractError, dec_str, frac_to_decimal, parse_date
from .evaluation import (cell_record, compare_with_b0, config_identity, interpret_b0_b1, qualify_config,
                         rank_configs)
from .factors import REGISTRY, validate_package
from .fees import FeeSchedule
from .replay import CellRunner, CellSpec
from .rules import RuleBook


def matrix_specs(study: dict, package: dict | None, allowed_boards, financial_codes, etf_ids, permission_id: str):
    end = parse_date(study["end_date"]) if study["end_date"] else None
    specs = []
    for role in study["roles"]:
        for cap in study["capitals"]:
            for y in study["start_years"]:
                for sc in study["scenarios"]:
                    specs.append(CellSpec(study["study_id"], role, cap, y, sc, end or date(1900, 1, 1),
                                          study["execution_mode"], package if role != "B0" else None,
                                          frozenset(allowed_boards), frozenset(financial_codes), frozenset(etf_ids),
                                          permission_id))
    return specs


def preflight(bundle: LoadedBundle, market) -> list[str]:
    """矩阵级阻断：任何一项存在时，所有单元不启动回放（证据不足），但仍逐格输出记录。"""
    s = bundle.study
    out = []
    if s["end_date"] is None:
        out.append("END_DATE_NOT_FIXED")
    if market is None:
        out.append("MARKET_DATA_NOT_PROVIDED")
    if s["execution_mode"] == "FORWARD_MANUAL":
        out.append("FORWARD_MANUAL_IS_NOT_A_REPLAY_MODE")
    for name, code in (("rules", "RULE_UNKNOWN"), ("fees", "FEE_SCHEDULE_INCOMPLETE"), ("universe", "UNIVERSE_NOT_PROVIDED"),
                       ("sources", "SOURCE_PROFILE_NOT_PROVIDED")):
        if bundle.payload(name) is None:
            out.append(code)
    if s["fact_snapshot_id"] is None:
        out.append("FACT_SNAPSHOT_NOT_FIXED")
    if market is not None and s["end_date"] is not None:
        end = parse_date(s["end_date"])
        if end > market.data_end:
            out.append("END_AFTER_DATA")                 # 不能延长D或用缺失数据补到D
        elif not market.calendar.is_session(end):
            out.append("END_DATE_NOT_A_SESSION")         # D须为共同可信完整数据的市场日终点
    uni = bundle.payload("universe")
    if market is not None and uni is not None and not check_exclusive_scopes(uni["etf_security_ids"],
                                                                             market.universe()).ok:
        out.append("SCOPE_OVERLAP")                      # ETF与股票范围必须互斥，拒绝共享运行
    return out


def cell_preflight(bundle: LoadedBundle, spec: CellSpec, market, start_day: date | None) -> list[str]:
    out = []
    if spec.role != "B0" and bundle.payload("factor") is None:
        out.append("FACTOR_PACKAGE_MISSING")
    elif spec.role != "B0":
        try:
            validate_package(spec.package)
        except ContractError as e:
            out.append(str(e))
        for f in spec.package["factors"]:
            if f["implementation_id"] not in REGISTRY:
                out.append(f"FACTOR_IMPLEMENTATION_MISSING:{f['implementation_id']}")
    if start_day is None:
        out.append("START_ANCHOR_UNAVAILABLE")
        return out
    fc = fee_coverage(bundle.payload("fees"), start_day, spec.end_date)
    out += [f"{r.code}:{r.scope_id}" for r in fc.reasons]
    boards = sorted({(m["exchange"], m["board"]) for m in market.master.values()
                     if m["board"] in spec.allowed_boards})
    rc = rule_coverage(bundle.payload("rules"), start_day, spec.end_date, boards)
    out += [f"{r.code}:{r.scope_id}" for r in rc.reasons]
    return out


def _run_one(args):
    spec, bundle_raw, market_factory, out_dir, persist_events = args
    from .bundle import load_bundle
    bundle = load_bundle(bundle_raw)
    market = market_factory()
    return run_cell_record(bundle, spec, market, out_dir, persist_events)


def input_versions(bundle: LoadedBundle, spec: CellSpec) -> dict:
    s = bundle.study
    pkg = spec.package if spec.role != "B0" else None
    return {"bundle_version": bundle.raw["bundle_version"], "study_id": s["study_id"],
            "fact_snapshot_id": s["fact_snapshot_id"], "source_profile_id": s["source_profile_id"],
            "universe_profile_id": s["universe_profile_id"], "rulebook_id": s["rulebook_id"],
            "fee_profile_id": s["fee_profile_id"], "bootstrap_id": s["bootstrap_id"],
            "package_id": (pkg or {}).get("package_id"), "package_purpose": (pkg or {}).get("purpose")}


def _safe(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key)


def write_ledger_files(L, d: Path) -> dict:
    """人工可读日账（ledger.csv）、成交（trades.csv）与周度计划（plans.jsonl）；比例列为展示值。"""
    import csv
    d.mkdir(parents=True, exist_ok=True)

    def f(x, places=10):
        return None if x is None else str(frac_to_decimal(x, places))
    with open(d / "ledger.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "equity", "nav", "hwm", "dd", "cash", "stock_value", "normal_stock_value", "names",
                    "valuation_status", "run_state", "frozen_exit_value"])
        for p in L.ledger_points:
            w.writerow([p["date"], f(p.get("equity"), 6), f(p.get("nav")), f(p.get("hwm")), f(p.get("dd")),
                        dec_str(p["cash"]), f(p.get("stock_value"), 6), f(p.get("normal_stock_value"), 6),
                        p.get("names"), p.get("valuation_status"), p.get("run_state"), f(p.get("frozen_exit_value"), 6)])
    with open(d / "trades.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "security_id", "side", "qty", "amount", "fee", "order", "occurred_at"])
        for t in L.trade_log:
            w.writerow([t["date"], t["sid"], t["side"], t["qty"], t["amount"], t["fee"], t["order"], t["occurred_at"]])
    with open(d / "plans.jsonl", "w", encoding="utf-8") as fh:
        for pid in sorted(L.plans):
            fh.write(json.dumps(L.plans[pid], ensure_ascii=False, sort_keys=True) + "\n")
    return {"ledger_path": str(d / "ledger.csv"), "plan_path": str(d / "plans.jsonl"),
            "trades_path": str(d / "trades.csv")}


def write_cell_artifacts(out_dir: str, spec: CellSpec, runner: CellRunner) -> dict:
    """逐格人工可读日账、成交与周度计划；完整事件仅在 persist_events 时写出（可由确定性回放重建）。"""
    import gzip
    d = Path(out_dir) / "cells" / _safe(spec.cell_key)
    out = dict(write_ledger_files(runner.ledger, d), event_path=None)
    if runner.event_log is not None:
        with gzip.open(d / "events.jsonl.gz", "wt", encoding="utf-8") as fh:
            for ev in runner.event_log:
                fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
        out["event_path"] = str(d / "events.jsonl.gz")
    return out


def run_cell_record(bundle: LoadedBundle, spec: CellSpec, market, out_dir: str | None = None,
                    persist_events: bool = False) -> dict:
    rb, fees = RuleBook(bundle.payload("rules")), FeeSchedule(bundle.payload("fees"))
    evidence = [r.code + ":" + r.scope_id for r in bundle.findings]
    try:
        runner = CellRunner(spec, market, rb, fees)
    except ContractError as e:
        return blocked_records(bundle, [spec], [str(e)])[0]
    start_day = runner.cal.first_week_end_in_year(spec.start_year, market.data_end)
    blocks = cell_preflight(bundle, spec, market, start_day)
    iv = input_versions(bundle, spec)
    if blocks:
        runner.startup = {"status": "NOT_STARTED_UNKNOWN", "reason": ";".join(blocks)}
        return cell_record(spec, runner, end_d=spec.end_date, evidence_reasons=evidence + blocks, input_versions=iv)
    if persist_events:
        runner.event_log = []
    err = None
    try:
        runner.run()
    except ContractError as e:
        err = str(e)                               # 业务阻断照实记录为该格证据不足，不中断整个矩阵
    arts = write_cell_artifacts(out_dir, spec, runner) if out_dir else {}
    return cell_record(spec, runner, end_d=spec.end_date, evidence_reasons=evidence, input_versions=iv,
                       artifacts=arts, run_error=err)


def verified_for_scope(bundle: LoadedBundle, name: str, start: date | None, end: date | None) -> bool:
    """外部事实只有 VERIFIED_FOR_SCOPE 且适用范围覆盖本矩阵区间的历史研究用途时才算已验证。"""
    p = bundle.profile(name)
    if p["status"] != "VERIFIED_FOR_SCOPE" or p["scope"]["purpose"] != "HISTORICAL_RESEARCH":
        return False
    sc = p["scope"]
    if start is not None and (sc["start_date"] is None or parse_date(sc["start_date"]) > start):
        return False
    if end is not None and (sc["end_date"] is None or parse_date(sc["end_date"]) < end):
        return False
    return True


def blocked_records(bundle: LoadedBundle, specs, blocks: list[str]) -> list[dict]:
    recs = []
    for spec in specs:
        recs.append({"cell_key": spec.cell_key, "role": spec.role, "capital": spec.capital,
                     "permission": spec.permission_id, "package": (spec.package or {}).get("package_id"),
                     "start": START_IDS[spec.start_year], "scenario": spec.scenario,
                     "execution_mode": spec.execution_mode, "start_cutoff": None, "end_D": bundle.study["end_date"],
                     "startup_status": "NOT_STARTED_UNKNOWN", "startup_reason": ";".join(blocks),
                     "evidence_reasons": blocks + [r.code + ":" + r.scope_id for r in bundle.findings],
                     "retirement_triggered": None, "CAGR_decimal": None, "MDD_exact": None,
                     "qualification_reasons": ["SCENARIO_INCOMPLETE"]})
    return recs


def run_matrix(bundle: LoadedBundle, market=None, market_factory=None, workers: int = 1, out_dir: str | None = None,
               permission_id: str = "perm-default", persist_events: bool = False) -> dict:
    s = bundle.study
    uni = bundle.payload("universe") or {}
    allowed = {p["board"] for p in uni.get("permissions", []) if p["status"] == "ALLOWED"}
    pkg = bundle.payload("factor")
    m0 = market if market is not None else (market_factory() if market_factory else None)
    fin = getattr(m0, "financial_codes", None)          # 金融业分类代码须由历史行业来源提供
    specs = matrix_specs(s, pkg, allowed, set(fin or ()), uni.get("etf_security_ids", []), permission_id)
    blocks = preflight(bundle, m0)
    if m0 is not None and not fin:
        blocks.append("FINANCIAL_CLASSIFICATION_NOT_PROVIDED")
    if blocks:
        records = blocked_records(bundle, specs, blocks)
    elif workers > 1 and market_factory is not None:
        with mp.get_context("fork").Pool(workers) as pool:
            records = pool.map(_run_one, [(sp, bundle.raw, market_factory, out_dir, persist_events) for sp in specs])
    else:
        m = market if market is not None else market_factory()
        records = [run_cell_record(bundle, sp, m, out_dir, persist_events) for sp in specs]
    test_pkg = (pkg or {}).get("purpose") == "TEST_ONLY" or s["purpose"] == "ENGINEERING_ONLY"
    configs = {}
    for r in records:
        key = (r["role"], r["capital"], r["permission"], r["package"], r["execution_mode"])
        configs.setdefault(key, []).append(r)
    starts = [r["start_cutoff"] for r in records if r.get("start_cutoff")]
    win = (parse_date(min(starts)) if starts else None, parse_date(s["end_date"]) if s["end_date"] else None)
    ext = {"sources": verified_for_scope(bundle, "sources", *win),
           "fees": verified_for_scope(bundle, "fees", *win),
           "rules": verified_for_scope(bundle, "rules", *win),
           "universe": verified_for_scope(bundle, "universe", *win),
           "shared_cash_and_execution": False}
    results = []
    for key, cells in sorted(configs.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        q = qualify_config(cells, test_package=test_pkg, external_verified=ext, d_fixed=s["end_date"] is not None,
                           benchmark_specified=bundle.payload("benchmark") is not None)
        ident = {"detail_version": bundle.raw["bundle_version"], "baseline": bundle.raw["baseline"],
                 "execution_technical": bundle.raw["execution_technical"], "role": key[0], "capital": key[1],
                 "allowed_boards": sorted(allowed), "execution_mode": key[4],
                 "package": pkg if key[0] != "B0" else None, "fees": bundle.payload("fees")}
        results.append({"config_key": "/".join(str(x) for x in key), "role": key[0], "capital": key[1],
                        "permission": key[2], "execution_mode": key[4],
                        "package_id": key[3], "factor_count": len((pkg or {}).get("factors", [])) if key[0] != "B0" else 0,
                        "config_identity": config_identity(ident), "qualification": q})
    ranking = rank_configs(results)
    out = {"study_id": s["study_id"], "cells": len(records), "matrix_blocks": blocks,
           "started_cells": sum(1 for r in records if r["startup_status"] == "STARTED"),
           "configs": [{k: v for k, v in r.items() if k != "config_identity"} for r in results],
           "ranking": ranking, "b0_comparisons": b0_comparisons(results, configs)}
    if out_dir:
        p = Path(out_dir)
        p.mkdir(parents=True, exist_ok=True)
        with open(p / "cells.jsonl", "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        (p / "qualification.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        # 配置语义身份（规范化内容，供停用后重新申请时比较；H08.3 不以文件名/ID代替）
        (p / "config_identities.json").write_text(json.dumps({r["config_key"]: json.loads(r["config_identity"])
                                                              for r in results}, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
    out["records"] = records
    return out


def b0_comparisons(results: list[dict], configs: dict) -> list[dict]:
    """D20.2：同金额/权限/执行配置下 B1 对 B0。只有两者都可审查时才比较 A0 的 BASE/HIGH；否则只给停用解读。"""
    out = []
    by = {(r["role"], r["capital"], r["permission"], r["execution_mode"]): r for r in results}
    for (role, cap, perm, mode), r1 in sorted(by.items()):
        if role != "B1" or ("B0", cap, perm, mode) not in by:
            continue
        r0 = by[("B0", cap, perm, mode)]
        item = {"capital": cap, "permission": perm, "execution_mode": mode,
                "interpretation": interpret_b0_b1(r0["qualification"], r1["qualification"]),
                "increment": "NOT_COMPARABLE"}
        if r0["qualification"]["status"] == r1["qualification"]["status"] == "REVIEWABLE":
            k1 = next(k for k in configs if "/".join(str(x) for x in k) == r1["config_key"])
            k0 = next(k for k in configs if "/".join(str(x) for x in k) == r0["config_key"])
            a1 = {c["scenario"]: c for c in configs[k1] if c["start"] == "A0"}
            a0 = {c["scenario"]: c for c in configs[k0] if c["start"] == "A0"}
            item["increment"] = compare_with_b0(a1, a0)
        out.append(item)
    return out
