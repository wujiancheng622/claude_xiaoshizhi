"""运行生产实现的独立测试，并把结果逐项映射到 338 条设计用例（PASS/FAIL/NOT_RUN/BLOCKED）。

不调用 handoff 中的 oracle；未被任何测试覆盖的用例保持 NOT_RUN，G18 外部验证保持 BLOCKED。
用法：python3 tools/run_acceptance.py [--out reports]
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLANNED = ROOT / "handoff" / "小市值周频策略_V5D2" / "verification" / "planned_test_cases.json"

G18_BLOCK_REASON = {
    "G18-01": "需要QMT与Tushare同日真实样本（价/量/额/股本/单位）",
    "G18-02": "需要QMT历史财务、披露与版本能力实测",
    "G18-03": "需要后来退市/停牌证券的真实历史身份与状态",
    "G18-04": "需要分红/送转/配股/换股多阶段真实样本",
    "G18-05": "需要用户券商部分成交、部撤、迟到回报、重复查询样本",
    "G18-06": "需要真实可用/冻结现金、回款与费用映射证据",
    "G18-07": "需要ETF自身权益计划与互斥范围实测",
    "G18-08": "需要人工串行两批及应急优先演练（不下真实单）",
    "G18-09": "需要历史费率与用户实际清算单",
    "G18-10": "需要分钟10:00—10:30与前向成交样本",
    "G18-11": "需要日历、交易单位、价格笼子、涨跌停、市值底线的已核验历史表",
    "G18-12": "需要真实初始已有仓的税批次/未结权益/委托",
}


class Collector(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.outcomes: dict[str, dict] = {}

    def _rec(self, test, status, detail=""):
        method = getattr(test, test._testMethodName, None) if hasattr(test, "_testMethodName") else None
        ids = getattr(method, "case_ids", ())
        self.outcomes[test.id()] = {"status": status, "case_ids": list(ids), "detail": detail}

    def addSuccess(self, test):
        super().addSuccess(test)
        self._rec(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._rec(test, "FAIL", "".join(traceback.format_exception(*err))[-2000:])

    def addError(self, test, err):
        super().addError(test, err)
        self._rec(test, "FAIL", "ERROR: " + "".join(traceback.format_exception(*err))[-2000:])

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._rec(test, "NOT_RUN", reason)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports"))
    ap.add_argument("--pattern", default="test_*.py")
    args = ap.parse_args(argv)
    t0 = time.time()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern, top_level_dir=str(ROOT))
    runner = unittest.TextTestRunner(resultclass=Collector, verbosity=1, stream=sys.stderr)
    result = runner.run(suite)
    planned = json.loads(PLANNED.read_text(encoding="utf-8"))["cases"]
    by_case: dict[str, list] = {c["id"]: [] for c in planned}
    unknown_ids = set()
    for tid, o in result.outcomes.items():
        for cid in o["case_ids"]:
            if cid in by_case:
                by_case[cid].append((tid, o["status"], o["detail"]))
            else:
                unknown_ids.add(cid)
    rows = []
    for c in planned:
        tests = by_case[c["id"]]
        if c["group"] == "G18" and not tests:
            status, note = "BLOCKED", G18_BLOCK_REASON.get(c["id"], "外部能力未验证")
        elif not tests:
            status, note = "NOT_RUN", "未实现对应生产测试"
        elif any(s == "FAIL" for _, s, _ in tests):
            status, note = "FAIL", "; ".join(d.splitlines()[-1] if d else "" for _, s, d in tests if s == "FAIL")
        elif all(s == "PASS" for _, s, _ in tests):
            status, note = "PASS", ""
        else:
            status, note = "NOT_RUN", "部分测试未运行"
        rows.append({"id": c["id"], "group": c["group"], "requirement": c["case_columns"], "status": status,
                     "tests": [t for t, _, _ in tests], "note": note})
    golden_ids = [c["id"] for c in json.loads((PLANNED.parent / "golden_vectors.json").read_text(encoding="utf-8"))["cases"]]
    gold = []
    for gid in golden_ids:
        hits = [(tid, o["status"]) for tid, o in result.outcomes.items() if gid in o["case_ids"]]
        st = "NOT_RUN" if not hits else ("PASS" if all(s == "PASS" for _, s in hits) else "FAIL")
        gold.append({"id": gid, "status": st, "tests": [t for t, _ in hits]})
    unknown_ids -= set(golden_ids)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "detail_version": "V5D2",
        "execution_environment": {"python": platform.python_version(), "platform": platform.platform(),
                                  "location": "user workstation (WSL)", "oracle_used": False},
        "unit_tests": {"run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                       "skipped": len(result.skipped), "seconds": round(time.time() - t0, 1)},
        "case_counts": counts, "total_cases": len(rows), "unknown_case_ids": sorted(unknown_ids),
        "golden_vectors": gold,
        "cases": rows,
        "test_outcomes": result.outcomes,
    }
    (out / "acceptance_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# V5D2 生产实现验收结果（自动生成）", "",
             f"- 生成时间：{report['generated_at']}",
             f"- 单元/集成测试方法：{result.testsRun} 个，失败 {len(result.failures)}，错误 {len(result.errors)}",
             f"- 338 条设计用例：" + "，".join(f"{k} {v}" for k, v in sorted(counts.items())),
             f"- 32 组黄金向量：" + "，".join(f"{k} {v}" for k, v in sorted(
                 {s: sum(1 for g in gold if g["status"] == s) for s in {g["status"] for g in gold}}.items())),
             "- 说明：未调用 handoff 参考 oracle；G18 外部验证无真实样本保持 BLOCKED；未覆盖用例保持 NOT_RUN。", "",
             "| 用例 | 状态 | 测试 | 备注 |", "|---|---|---|---|"]
    for r in rows:
        tests = "<br>".join(t.split(".")[-1] for t in r["tests"][:3])
        lines.append(f"| {r['id']} | {r['status']} | {tests} | {r['note'][:120]} |")
    (out / "acceptance_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"case_counts": counts, "unit_tests": report["unit_tests"],
                      "golden": {g["id"]: g["status"] for g in gold if g["status"] != "PASS"},
                      "unknown_case_ids": sorted(unknown_ids)}, ensure_ascii=False))
    return 0 if not (result.failures or result.errors) else 1


if __name__ == "__main__":
    sys.exit(main())
