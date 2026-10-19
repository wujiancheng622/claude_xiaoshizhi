"""把一次矩阵输出（cells.jsonl + qualification.json）汇总成人工可读的 Markdown。

用法：python3 tools/summarize_matrix.py runs/<dir> [--title ...]
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--title", default="矩阵汇总")
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    d = Path(a.run_dir)
    recs = [json.loads(x) for x in (d / "cells.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    q = json.loads((d / "qualification.json").read_text(encoding="utf-8"))
    lines = [f"# {a.title}", ""]
    if a.note:
        lines += [a.note, ""]
    st = collections.Counter(r["startup_status"] for r in recs)
    lines += [f"- 单元数：{len(recs)}；启动：{st.get('STARTED', 0)}；未启动-不适配：{st.get('NOT_STARTED_UNFIT', 0)}；"
              f"未启动-证据不足：{st.get('NOT_STARTED_UNKNOWN', 0)}",
              f"- 真实触发25%应急停用的单元：{sum(1 for r in recs if r.get('retirement_triggered'))}",
              f"- 运行中业务阻断（RUN_ABORTED）的单元：{sum(1 for r in recs if r.get('run_error'))}",
              f"- 矩阵级阻断：{', '.join(q['matrix_blocks']) or '无'}",
              f"- 研究排序结论：{q['ranking']['conclusion']}", "",
              "| 角色 | 金额 | 起点 | 情景 | 启动 | 停用 | 触发时刻 | CAGR | MDD | 覆盖可用性 | 成交笔数 | 平均持股 | 受阻退出最大比 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(recs, key=lambda r: (r["role"], int(r["capital"]), r["start"], r["scenario"])):
        cagr = r.get("CAGR_decimal")
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["role"], r["capital"], r["start"], r["scenario"],
            r["startup_status"] + (f"（{r.get('startup_reason')}）" if r["startup_status"] != "STARTED" else ""),
            "是" if r.get("retirement_triggered") else ("否" if r.get("retirement_triggered") is False else "—"),
            (r.get("trigger_at") or "")[:10], f"{float(cagr):.4%}" if cagr else "—",
            r.get("MDD_display") or "—", r.get("package_availability") or "—", r.get("fills") or "—",
            r.get("average_names") or "—", r.get("frozen_exit_ratio_max") or "—"))
    lines += ["", "## 配置资格", "", "| 配置 | 状态 | 已确认失败（前6项） |", "|---|---|---|"]
    for c in q["configs"]:
        lines.append(f"| {c['config_key']} | {c['qualification']['status']} | "
                     f"{'; '.join(c['qualification']['failures'][:6]) or '—'} |")
    if q.get("b0_comparisons"):
        lines += ["", "## B1 对 B0（D20.2）", "", "| 金额 | 执行模式 | 增量判断 | 停用解读 |", "|---|---|---|---|"]
        for b in q["b0_comparisons"]:
            lines.append(f"| {b['capital']} | {b['execution_mode']} | {b['increment']} | {b['interpretation']} |")
    out = d / "summary.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
