"""命令入口（D21.1）。本交付不提供 send-live-orders；任何命令都不调用真实买卖接口。

python3 -m smallcap validate-inputs --bundle B.json
python3 -m smallcap build-facts --bundle B.json --raw rows.jsonl --out facts/
python3 -m smallcap replay --bundle B.json --out runs/X [--synthetic-engineering] [--workers N] [--persist-events]
python3 -m smallcap plan --bundle B.json --journal runs/acct
python3 -m smallcap import-broker-events --bundle B.json --journal runs/acct --file fills.csv
python3 -m smallcap reconcile --bundle B.json --journal runs/acct --broker broker.json
python3 -m smallcap resume --bundle B.json --journal runs/acct
python3 -m smallcap evaluate --cells runs/X/cells.jsonl [--bundle B.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .bundle import load_bundle
from .contracts import ContractError
from .forward import (dispatch_mode, forward_ledger, forward_preconditions, import_broker_events, manual_checklist,
                      read_rows, reconcile_account, resume_actions)
from .journal import Journal


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def cmd_validate(a) -> int:
    try:
        b = load_bundle(a.bundle)
    except ContractError as e:
        _print({"status": "INVALID", "code": e.code, "detail": str(e)})
        return 2
    _print({"status": "STRUCTURE_OK_NOT_SOURCE_CERTIFICATION", "study": {k: v for k, v in b.study.items()
                                                                         if k not in ("baseline", "execution_technical")},
            "findings": [r.to_json() for r in b.findings]})
    return 0


def _synthetic_factory(seed: int, end: str):
    import functools
    from .synthetic import make_synthetic
    return functools.partial(make_synthetic, seed, end)


def cmd_build_facts(a) -> int:
    """只读：把已导出的 QMT/Tushare 原始行按 SourcesProfile 映射规范化并一次裁决来源，输出来源轨迹。"""
    from .data import build_facts
    b = load_bundle(a.bundle)
    src = b.payload("sources")
    if src is None:
        _print({"status": "BLOCKED", "reason": "SOURCE_PROFILE_NOT_PROVIDED"})
        return 3
    p = Path(a.raw)
    if p.suffix == ".jsonl":
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    else:
        rows = read_rows(p)
    obs, decisions = build_facts(rows, src["mappings"])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "observations.jsonl", "w", encoding="utf-8") as fh:
        for o in obs:
            fh.write(json.dumps({k: (format(v, "f") if hasattr(v, "as_tuple") else
                                     (v.isoformat() if hasattr(v, "isoformat") else v))
                                 for k, v in o.__dict__.items()}, ensure_ascii=False, sort_keys=True) + "\n")
    with open(out / "facts.jsonl", "w", encoding="utf-8") as fh:
        for d in decisions:
            fh.write(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")
    import collections
    _print({"status": "OK_NOT_SOURCE_CERTIFICATION", "observations": len(obs),
            "decisions": dict(collections.Counter(d["action"] for d in decisions)), "out": str(out)})
    return 0


def cmd_replay(a) -> int:
    from .matrix import run_matrix
    b = load_bundle(a.bundle)
    mode = dispatch_mode(b.study["execution_mode"])
    if not mode.ok:
        _print({"status": "REFUSED", "reasons": [r.to_json() for r in mode.reasons]})
        return 2
    factory = None
    if a.research_store:
        if b.study["purpose"] == "DEPLOYMENT_CANDIDATE":
            _print({"status": "REFUSED", "reason": "临时 Tushare 研究数据仓不能用于部署候选研究"})
            return 2
        import functools
        from .research_market import ResearchMarket
        factory = functools.partial(ResearchMarket, a.research_store)
    elif a.synthetic_engineering:
        if b.study["purpose"] != "ENGINEERING_ONLY":
            _print({"status": "REFUSED", "reason": "合成市场只允许 ENGINEERING_ONLY 研究"})
            return 2
        factory = _synthetic_factory(a.seed, b.study["end_date"] or "2026-10-16")
    out = run_matrix(b, market_factory=factory, workers=a.workers, out_dir=a.out, persist_events=a.persist_events,
                     only_roles=a.roles.split(",") if a.roles else None)
    out.pop("records", None)
    _print(out)
    return 0


def cmd_plan(a) -> int:
    """FORWARD_MANUAL：按相位推进（INIT/PREOPEN/EXEC/CLOSE/CUTOFF）；EXEC 只生成预约委托与人工清单。"""
    b = load_bundle(a.bundle)
    blocks = forward_preconditions(b)
    mode = dispatch_mode(b.study["execution_mode"])
    if not mode.ok:
        blocks += mode.reasons
    hard = [r for r in blocks if not r.code.startswith("PERMISSION_")]
    if hard or b.study["execution_mode"] != "FORWARD_MANUAL" or not a.snapshot or not a.journal:
        _print(manual_checklist(None, [], blocks) | {"status": "BLOCKED",
                                                     "need": "FORWARD_MANUAL Bundle + --journal + --snapshot（只读事实）"})
        return 3
    from datetime import date as _date
    from .forward import ForwardSession
    from .snapshot import SnapshotMarket
    j = _journal(a)
    try:
        fs = ForwardSession(b, j, SnapshotMarket(a.snapshot))
        out = fs.step(_date.fromisoformat(a.date), a.phase)
        from .matrix import write_ledger_files
        out.update(write_ledger_files(fs.runner.ledger, Path(a.journal) / "reports"))
        if out.get("checklist"):
            p = Path(a.journal) / "plans" / f"checklist-{a.date}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(dict(manual_checklist(None, out["checklist"], blocks),
                                         final_intents=out.get("final_intents", [])), ensure_ascii=False, indent=1),
                         encoding="utf-8")
            out["checklist_file"] = str(p)
        _print(out)
    finally:
        j.release()
    return 0


def _journal(a):
    j = Journal(a.journal)
    j.acquire()
    return j


def cmd_import(a) -> int:
    b = load_bundle(a.bundle)
    j = _journal(a)
    try:
        lg = forward_ledger(b, j)
        rows = read_rows(a.file)
        if a.status_map:
            from .forward import map_broker_status_rows
            rows = map_broker_status_rows(rows, json.loads(Path(a.status_map).read_text(encoding="utf-8")))
        res = import_broker_events(lg, j, rows)
        j.write_snapshot(lg)
        _print(res)
    finally:
        j.release()
    return 0 if not res["conflicts"] else 4


def cmd_reconcile(a) -> int:
    b = load_bundle(a.bundle)
    j = _journal(a)
    try:
        lg = forward_ledger(b, j)
        broker = json.loads(Path(a.broker).read_text(encoding="utf-8"))
        r = reconcile_account(lg, broker, broker.get("explained_cash"), j)
        j.write_snapshot(lg)
        _print({"status": r.status, "differences": [x.to_json() for x in r.reasons]})
    finally:
        j.release()
    return 0 if r.ok else 5


def cmd_resume(a) -> int:
    b = load_bundle(a.bundle)
    j = _journal(a)
    try:
        lg = forward_ledger(b, j)
        _print(resume_actions(lg, datetime.now(timezone.utc)))
    finally:
        j.release()
    return 0


def cmd_evaluate(a) -> int:
    """固定全矩阵资格及排序；无 --bundle 时外部事实一律按未验证处理。"""
    from .evaluation import qualify_config, rank_configs
    from .contracts import parse_date
    cells = [json.loads(x) for x in Path(a.cells).read_text(encoding="utf-8").splitlines() if x.strip()]
    configs = {}
    for c in cells:
        configs.setdefault((c["role"], c["capital"], c["permission"], c["package"], c["execution_mode"]), []).append(c)
    ext, d_fixed, bench, test_pkg = {"all": False}, True, False, a.test_package
    if a.bundle:
        from .matrix import verified_for_scope
        b = load_bundle(a.bundle)
        starts = [c["start_cutoff"] for c in cells if c.get("start_cutoff")]
        win = (parse_date(min(starts)) if starts else None,
               parse_date(b.study["end_date"]) if b.study["end_date"] else None)
        ext = {n: verified_for_scope(b, n, *win) for n in ("sources", "fees", "rules", "universe")}
        ext["shared_cash_and_execution"] = False
        d_fixed, bench = b.study["end_date"] is not None, b.payload("benchmark") is not None
        test_pkg = test_pkg or b.study["purpose"] == "ENGINEERING_ONLY" or \
            (b.payload("factor") or {}).get("purpose") == "TEST_ONLY"
    res = []
    for k, cs in sorted(configs.items(), key=lambda kv: tuple(map(str, kv[0]))):
        res.append({"config_key": "/".join(map(str, k)), "role": k[0],
                    "qualification": qualify_config(cs, test_package=test_pkg, external_verified=ext,
                                                    d_fixed=d_fixed, benchmark_specified=bench)})
    _print({"configs": res, "ranking": rank_configs(res)})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="smallcap", description="小市值周频策略 V5D2（无真实报撤单）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate-inputs")
    p.add_argument("--bundle", required=True)
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("build-facts", help="只读：来源行规范化与来源裁决（不联网、不读凭据）")
    p.add_argument("--bundle", required=True)
    p.add_argument("--raw", required=True, help="已导出的来源行 .jsonl/.json/.csv")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_build_facts)
    p = sub.add_parser("replay")
    p.add_argument("--bundle", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--synthetic-engineering", action="store_true",
                   help="使用 TEST_ONLY 合成市场做工程联调（结果不构成投资证据）")
    p.add_argument("--persist-events", action="store_true", help="逐格写出完整事件 events.jsonl.gz")
    p.add_argument("--research-store", help="研究数据仓目录（临时 Tushare 来源，research/data_prep/build_store.py 生成）")
    p.add_argument("--roles", help="只跑指定角色，逗号分隔（研究便利，如 B1,B2）")
    p.set_defaults(fn=cmd_replay)
    p = sub.add_parser("plan")
    p.add_argument("--bundle", required=True)
    p.add_argument("--journal")
    p.add_argument("--snapshot", help="只读事实快照 JSON（SMALLCAP_SNAPSHOT）")
    p.add_argument("--date")
    p.add_argument("--phase", choices=["INIT", "PREOPEN", "EXEC", "CLOSE", "CUTOFF"])
    p.set_defaults(fn=cmd_plan)
    for name, fn in (("import-broker-events", cmd_import), ("reconcile", cmd_reconcile), ("resume", cmd_resume)):
        p = sub.add_parser(name)
        p.add_argument("--bundle", required=True, help="FORWARD_MANUAL Bundle（费用表/ETF范围/账户身份）")
        p.add_argument("--journal", required=True)
        if name == "import-broker-events":
            p.add_argument("--file", required=True)
            p.add_argument("--status-map", help="券商委托状态值→标准状态的映射 JSON（须有实际SDK版本样例）")
        if name == "reconcile":
            p.add_argument("--broker", required=True)
        p.set_defaults(fn=fn)
    p = sub.add_parser("evaluate")
    p.add_argument("--cells", required=True)
    p.add_argument("--bundle", help="用于外部事实适用范围、终点D与ETF比较对象的状态")
    p.add_argument("--test-package", action="store_true")
    p.set_defaults(fn=cmd_evaluate)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
