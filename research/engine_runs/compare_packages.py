"""汇总 4 个候选包（及 B0）的 V5D2 研究矩阵结果，按预登记第 5 节的选择规则排序。"""
import json
import sys
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKGS = ["P1_SIZE_EP_VOL", "P2_SIZE_EP_TURN", "P3_EP_VOL_TURN", "P4_SIZE_EP_VOL_TURN", "P5_SIZE_EP_POSTHOC"]


def load():
    rows = []
    for p in PKGS:
        f = ROOT / "runs" / f"research_{p}" / "cells.jsonl"
        if not f.exists():
            continue
        for x in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(x)
            r["_pkg"] = "B0" if r["role"] == "B0" else p
            rows.append(r)
    return rows


def frac(w):
    return Fraction(int(w["num"]), int(w["den"])) if w else None


def summarize(rows):
    g = defaultdict(list)
    for r in rows:
        g[(r["_pkg"], r["role"], r["capital"])].append(r)
    out = []
    for (pkg, role, cap), cs in sorted(g.items()):
        started = [c for c in cs if c["startup_status"] == "STARTED"]
        unfit = [c for c in cs if c["startup_status"] == "NOT_STARTED_UNFIT"]
        retired = [c for c in started if c.get("retirement_triggered")]
        mdds = [frac(c["MDD_exact"]) for c in started if c.get("MDD_exact")]
        s01 = [c for c in started if c["scenario"] in ("S0", "S1")]
        cagr = [Decimal(c["CAGR_decimal"]) for c in s01 if c.get("CAGR_decimal") is not None]
        fees = [Decimal(c["annual_fee_rate"]) for c in s01 if c.get("annual_fee_rate")]
        alive = [c for c in s01 if not c.get("retirement_triggered")]
        out.append({"pkg": pkg, "role": role, "capital": cap, "cells": len(cs), "started": len(started),
                    "unfit": len(unfit), "retired": len(retired),
                    "retired_starts": sorted({c["start"] for c in retired}),
                    "worst_mdd": float(max(mdds)) if mdds else None,
                    "robust_cagr": float(min(cagr)) if cagr else None,
                    "s01_cagr_alive_min": float(min(Decimal(c["CAGR_decimal"]) for c in alive)) if alive else None,
                    "max_fee_rate": float(max(fees)) if fees else None,
                    "by_start": {s: {sc: (c.get("CAGR_decimal"), c.get("MDD_display"), c.get("retirement_triggered"),
                                          (c.get("trigger_at") or "")[:10])
                                     for c in cs if c["start"] == s for sc in [c["scenario"]]}
                                 for s in ("A0", "A1", "A2", "A3")},
                    "availability": sorted({c.get("package_availability") for c in started})})
    return out


def main():
    rows = load()
    res = summarize(rows)
    (ROOT / "research_out" / "engine_package_compare.json").write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                                                       encoding="utf-8")
    print("| 包 | 角色 | 金额 | 启动/16 | 不适配 | 停用格 | 停用起点 | 16格最坏回撤 | 稳健年化(S0/S1最小) | 最高年费用率 | 覆盖可用性 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in res:
        f = lambda x, p=1: "—" if x is None else f"{x:.{p}%}"
        print(f"| {r['pkg']} | {r['role']} | {r['capital']} | {r['started']} | {r['unfit']} | {r['retired']} | "
              f"{','.join(r['retired_starts']) or '—'} | {f(r['worst_mdd'])} | {f(r['robust_cagr'])} | "
              f"{f(r['max_fee_rate'], 2)} | {','.join(str(x) for x in r['availability'])} |")


if __name__ == "__main__":
    main()
