"""指标、全期绩效、运行可用性与采用资格（D19、D20、H08.2—8.3）。

NAV/HWM/DD/MDD 用精确有理数；CAGR 等展示/排序指标用 50 位 Decimal，不反写风险状态。
先判资格（真实性→可运行性→存续→风险→经济性），再排收益；测试包始终 ENGINEERING_ONLY。
"""
from __future__ import annotations

import decimal
from datetime import date, datetime, time
from decimal import Decimal
from fractions import Fraction

from .contracts import DD_LIMIT, DD_RETIRE, ROLES, SCENARIOS, START_IDS, frac_to_decimal, sh_dt, unwire_ratio, wire_ratio
from .factors import package_availability

YEAR_SECONDS = Decimal("365.2425") * 86400


def max_drawdown(series: list[tuple[str, Fraction]]) -> dict:
    """series: [(date, nav)]；峰值取最早达到该高点的日期，谷值取首次最大回撤日期，恢复为之后首次 NAV≥峰值。"""
    peak, peak_date = None, None
    best = Fraction(0)
    out = {"mdd": Fraction(0), "peak_date": None, "trough_date": None, "recovery_date": None, "recovered": True}
    cur_peak_date_for_best = None
    for d, nav in series:
        if peak is None or nav > peak:
            peak, peak_date = nav, d
        dd = 1 - nav / peak if peak > 0 else Fraction(0)
        if dd > best:
            best = dd
            out.update(mdd=dd, peak_date=peak_date, trough_date=d)
            cur_peak_date_for_best = peak
    if out["trough_date"] is not None:
        out["recovered"] = False
        after = False
        for d, nav in series:
            if d == out["trough_date"]:
                after = True
                continue
            if after and nav >= cur_peak_date_for_best:
                out["recovery_date"], out["recovered"] = d, True
                break
    return out


def window_slice(series: list[tuple[str, Fraction]], start: str, end: str | None = None) -> dict:
    """从长历史账户截出的时间窗只能标为切片，不能冒充该年现金新投入起点（D16.2）。"""
    pts = [(d, v) for d, v in series if d >= start and (end is None or d <= end)]
    return {"kind": "TIME_WINDOW_SLICE", "is_new_capital_start": False, "points": pts,
            "note": "不得替代独立入金的 A1/A2/A3 单元"}


def cagr(nav0: Fraction, navd: Fraction, start: datetime, end: datetime) -> Decimal | None:
    """CAGR=(NAV_D/NAV_0)^(1/Y)−1，Y=(D的日终时刻−入金时刻)/365.2425天；Y≤0或NAV≤0不输出常规复利年化。"""
    y = Decimal((end - start).total_seconds()) / YEAR_SECONDS
    if y <= 0 or navd <= 0 or nav0 <= 0:
        return None
    with decimal.localcontext() as c:
        c.prec = 50
        r = Decimal(navd.numerator) / Decimal(navd.denominator) / (Decimal(nav0.numerator) / Decimal(nav0.denominator))
        return (r.ln() / y).exp() - 1


def time_weighted_equity(points: list[dict], start: datetime, end: datetime) -> Decimal | None:
    """以前一条已完成权益为分段常数积分（休市沿用上一权益，不用未来权益回填）。"""
    tot, last_t, last_e = Decimal(0), None, None
    for pt in points:
        if pt.get("equity") is None:
            continue
        t = sh_dt(date.fromisoformat(pt["date"]), time(20, 0))
        if last_t is not None:
            tot += Decimal((t - last_t).total_seconds()) * last_e
        last_t, last_e = t, Decimal(pt["equity"].numerator) / Decimal(pt["equity"].denominator)
    if last_t is None:
        return None
    tot += Decimal((end - last_t).total_seconds()) * last_e
    span = Decimal((end - start).total_seconds())
    return tot / span if span > 0 else None


def _dec(x: Fraction, places: int = 10) -> str:
    return str(frac_to_decimal(x, places))


def annual_returns(series: list[tuple[str, Fraction]]) -> dict:
    """D19.2 年度收益：按上一年末NAV链接，第一年用入金NAV。"""
    out, prev, last_year, last_nav = {}, series[0][1] if series else None, None, None
    for d, nav in series:
        y = d[:4]
        if last_year is not None and y != last_year:
            out[last_year] = _dec(last_nav / prev - 1, 8)
            prev = last_nav
        last_year, last_nav = y, nav
    if last_year is not None:
        out[last_year] = _dec(last_nav / prev - 1, 8)
    return out


def annualized_volatility(series: list[tuple[str, Fraction]], years: Decimal) -> str | None:
    """观察值：实际市场日收益样本标准差×sqrt(当期平均每年市场会话数)；不影响采用硬条件。"""
    if len(series) < 3 or years <= 0:
        return None
    with decimal.localcontext() as c:
        c.prec = 40
        rs = [Decimal(n.numerator) / Decimal(n.denominator) / (Decimal(p.numerator) / Decimal(p.denominator)) - 1
              for (_, p), (_, n) in zip(series, series[1:]) if p > 0]
        if len(rs) < 2:
            return None
        m = sum(rs, Decimal(0)) / len(rs)
        var = sum(((r - m) ** 2 for r in rs), Decimal(0)) / (len(rs) - 1)
        per_year = Decimal(len(rs)) / years
        return str((var.sqrt() * per_year.sqrt()).quantize(Decimal("1e-8")))


def provisional_decisive(points: list[dict], base_mdd: Fraction, retired: bool) -> list[str]:
    """D12.3：暂估市值换成已知下界（无下界证据时按0，股票价值不为负）后，若可能跨越25%应急或30%MDD门槛，
    输出决定性证据不足。上界未知不影响这一下行检验，也不据此推算“影响很小”。"""
    out, run_max, mdd_lo = [], None, Fraction(0)
    crossed_retire = False
    for p in points:
        nav, units = p.get("nav"), p.get("units")
        if nav is None:
            continue
        run_max = nav if run_max is None or nav > run_max else run_max
        if p.get("provisional_value") and units:
            e_lo = p["equity"] - p["provisional_value"] + (p.get("provisional_lo") or Fraction(0))
            nav_lo = e_lo / units
        else:
            nav_lo = nav
        dd_lo = 1 - nav_lo / run_max if run_max > 0 else Fraction(0)
        mdd_lo = max(mdd_lo, dd_lo)
        hwm = p.get("hwm")
        if hwm and p.get("provisional_value") and units and 1 - nav_lo / hwm >= DD_RETIRE:
            crossed_retire = True
    if not retired and crossed_retire:
        out.append("PROVISIONAL_VALUATION_DECISIVE:RETIREMENT_25")
    if base_mdd <= DD_LIMIT < mdd_lo:
        out.append("PROVISIONAL_VALUATION_DECISIVE:MDD_30")
    return out


def exposure_report(week_stats: list[dict]) -> dict | None:
    """D19.4：市值几何均值、中位数（周中位数的均值）、池内分位、行业权重与未知部分（运行期内各周平均）。"""
    ws = [w["exposure"] for w in week_stats if w.get("exposure") and not w.get("retired") and w["exposure"]["names"]]
    if not ws:
        return None
    with decimal.localcontext() as c:
        c.prec = 40
        n_mv = sum(w["n_mv"] for w in ws)
        geo = (sum((Decimal(w["log_mv_mean"]) * w["n_mv"] for w in ws if w["n_mv"]), Decimal(0)) / n_mv).exp() \
            if n_mv else None
        meds = [Decimal(w["median_mv"]) for w in ws if w.get("median_mv")]
    pcts = [(w["in_pool"], unwire_ratio(w["pool_pct_mean"])) for w in ws if w.get("pool_pct_mean")]
    inds: dict = {}
    k = 0
    unk = Fraction(0)
    for w in ws:
        if "industry_weights" not in w:
            continue
        k += 1
        for name, v in w["industry_weights"].items():
            inds[name] = inds.get(name, Fraction(0)) + unwire_ratio(v)
        unk += unwire_ratio(w["unknown_industry_weight"])
    pool_pct = sum((n * x for n, x in pcts), Fraction(0)) / sum(n for n, _ in pcts) if pcts else None
    return {"weeks": len(ws), "mv_geometric_mean": str(geo.quantize(Decimal(1))) if geo is not None else None,
            "mv_median_weekly_mean": str((sum(meds, Decimal(0)) / len(meds)).quantize(Decimal(1))) if meds else None,
            "pool_percentile_mean": _dec(pool_pct, 6) if pool_pct is not None else None,
            "industry_weights_mean": {n: _dec(v / k, 6) for n, v in sorted(inds.items())} if k else {},
            "unknown_industry_weight_mean": _dec(unk / k, 6) if k else None,
            "unknown_mv_observations": sum(w["unknown_mv"] for w in ws),
            "note": "报告诊断，不凭回归残差宣称组合中性"}


def cell_record(spec, runner, *, end_d: date, evidence_reasons: list | None = None,
                input_versions: dict | None = None, artifacts: dict | None = None,
                run_error: str | None = None) -> dict:
    """H08.2 单元输出：所有格都产记录，即使未启动或数据不足。"""
    L = runner.ledger
    start_day = runner.cal.first_week_end_in_year(spec.start_year, runner.m.data_end)
    artifacts = artifacts or {}
    rec = {"cell_key": spec.cell_key, "role": spec.role, "capital": spec.capital, "permission": spec.permission_id,
           "package": (spec.package or {}).get("package_id") if spec.role != "B0" else "B0_SIZE",
           "start": START_IDS[spec.start_year], "scenario": spec.scenario, "execution_mode": spec.execution_mode,
           "input_versions": dict(input_versions or {}),
           "start_cutoff": start_day.isoformat() if start_day else None, "end_D": end_d.isoformat(),
           "startup_status": runner.startup.get("status"), "startup_reason": runner.startup.get("reason"),
           "start_pass": runner.startup.get("status") == "STARTED",
           "evidence_reasons": list(evidence_reasons or []), "run_error": run_error,
           "retirement_triggered": L.retirement_triggered, "trigger_at": L.retirement_trigger_at,
           "retired_at": None, "run_state_end": L.run_state,
           "ledger_path": artifacts.get("ledger_path"), "event_path": artifacts.get("event_path"),
           "plan_path": artifacts.get("plan_path")}
    if spec.execution_mode == "REPLAY_DAILY_PROXY":
        rec["evidence_reasons"].append("NOT_WINDOW_CERTIFIED")
    pts = [p for p in L.ledger_points if p.get("nav") is not None]
    if rec["startup_status"] != "STARTED" or not pts:
        rec.update(CAGR_decimal=None, MDD_exact=None, qualification_reasons=[rec["startup_status"]])
        return rec
    start_dt = sh_dt(start_day, time(20, 0))
    end_dt = sh_dt(end_d, time(20, 0))
    series = [(p["date"], p["nav"]) for p in pts]
    md = max_drawdown(series)
    c = cagr(series[0][1], series[-1][1], start_dt, end_dt)
    fees_by_kind: dict = {}
    for o in L.orders.values():
        for k, v in o.fees_accrued.items():
            fees_by_kind[k] = fees_by_kind.get(k, Decimal(0)) + v
    fee_total = sum(fees_by_kind.values(), Decimal(0))
    eq_by_date = {p["date"]: p["equity"] for p in L.ledger_points if p.get("equity") is not None}
    dates = sorted(eq_by_date)
    turnover_sum = Decimal(0)
    buy_amt = sell_amt = Decimal(0)
    import bisect
    for tr in L.trade_log:
        i = bisect.bisect_left(dates, tr["date"]) - 1
        if i < 0:
            continue
        e = eq_by_date[dates[i]]
        amt = Decimal(tr["amount"])
        turnover_sum += amt / (2 * Decimal(e.numerator) / Decimal(e.denominator))
        if tr["side"] == "BUY":
            buy_amt += amt
        else:
            sell_amt += amt
    y = Decimal((end_dt - start_dt).total_seconds()) / YEAR_SECONDS
    twa = time_weighted_equity(L.ledger_points, start_dt, end_dt)
    trig = L.retirement_trigger_at
    operating = [p for p in pts if trig is None or p["date"] < trig[:10]]
    in_mkt = sum(1 for p in pts if p.get("stock_value") and p["stock_value"] > 0)
    stock_ratios = [p["stock_value"] / p["equity"] for p in operating if p.get("stock_value") is not None and p["equity"] > 0]
    weeks = [w for w in L.week_stats]
    avail = package_availability([w["status"] for w in weeks]) if spec.role != "B0" else None
    unfinished = [o.client_order_id for o in L.open_orders()]
    residuals = [s for s, p in L.positions.items() if p.quantity > 0 and p.lifecycle == "RESIDUAL"]
    retired_at = next((pt["date"] for pt in L.ledger_points if pt.get("run_state") == "RETIRED"), None)
    # D19.3 运行持续性：应急触发前的市场日；正常/受限天数；持有正常股票的市场日比例
    normal_days = sum(1 for p in operating if p.get("run_state") == "NORMAL")
    in_mkt = sum(1 for p in pts if p.get("normal_stock_value"))
    names = [p["names"] for p in operating if p.get("names") is not None]
    frozen = [p["frozen_exit_value"] / p["equity"] for p in operating
              if p.get("frozen_exit_value") is not None and p["equity"] > 0]
    unrecovered = None
    if not md["recovered"] and md["peak_date"] is not None:
        unrecovered = sum(1 for d, _ in series if d > md["peak_date"])
    rec.update({
        "CAGR_decimal": str(c.quantize(Decimal("1e-10"))) if c is not None else None,
        "MDD_exact": wire_ratio(md["mdd"]), "MDD_display": str(frac_to_decimal(md["mdd"], 8)),
        "worst_peak": md["peak_date"], "worst_trough": md["trough_date"], "worst_recovery": md["recovery_date"],
        "recovered": md["recovered"], "nav_end": wire_ratio(series[-1][1]),
        "healthy_week_count": avail["healthy_weeks"] if avail else None,
        "assessed_week_count": avail["assessed_weeks"] if avail else len(weeks),
        "unknown_week_count": avail["unassessed_weeks"] if avail else None,
        "longest_bad_streak": avail["longest_bad_streak"] if avail else None,
        "package_availability": avail["status"] if avail else "NOT_APPLICABLE_B0",
        "fee_total": str(fee_total), "fee_by_kind": {k: str(v) for k, v in sorted(fees_by_kind.items())},
        "annual_fee_rate": str((fee_total / twa / y).quantize(Decimal("1e-10"))) if twa and y > 0 else None,
        "turnover_annual": str((turnover_sum / y).quantize(Decimal("1e-8"))) if y > 0 else None,
        "buy_amount": str(buy_amt), "sell_amount": str(sell_amt), "fills": len(L.trade_log), "orders": len(L.orders),
        "operating_days": len(operating), "normal_days": normal_days, "limited_days": len(operating) - normal_days,
        "first_execution": L.trade_log[0]["date"] if L.trade_log else None,
        "in_market_fraction": _dec(Fraction(in_mkt, len(pts)), 6) if pts else None,
        "average_stock_ratio": str(frac_to_decimal(sum(stock_ratios, Fraction(0)) / len(stock_ratios), 6)) if stock_ratios else None,
        "average_names": _dec(Fraction(sum(names), len(names)), 4) if names else None,
        "healthy_week_ratio": _dec(Fraction(avail["healthy_weeks"], avail["assessed_weeks"]), 6)
        if avail and avail["assessed_weeks"] else None,
        "max_bad_coverage_streak": avail["longest_bad_streak"] if avail else None,
        "uncertain_weeks": avail["unassessed_weeks"] if avail else None,
        "frozen_exit_ratio_max": _dec(max(frozen), 6) if frozen else None,
        "frozen_exit_ratio_avg": _dec(sum(frozen, Fraction(0)) / len(frozen), 6) if frozen else None,
        "annual_returns": annual_returns(series), "volatility_annualized_observation": annualized_volatility(series, y),
        "unrecovered_sessions": unrecovered,
        "unfinished_orders": unfinished, "residual_rights": residuals, "end_residuals": residuals,
        "retired_at": retired_at, "mdd_over_30": md["mdd"] > DD_LIMIT,
        "exposure": exposure_report(L.week_stats),
        "tax_or_fee_unknown": sorted(k for k in L.limitations if k.startswith(("TAX_UNKNOWN", "FEE_UNKNOWN"))),
        "decisive_unknowns": provisional_decisive(L.ledger_points, md["mdd"], L.retirement_triggered),
        "provisional_valuation_days": sum(1 for p in pts if p.get("provisional_value")),
    })
    reasons = []
    if L.retirement_triggered:
        reasons.append("RETIRED_TRIGGERED")
    if md["mdd"] > DD_LIMIT:
        reasons.append("MDD_OVER_30")
    rec["qualification_reasons"] = reasons
    return rec


# ---------------------------------------------------------------- 资格
def qualify_config(cells: list[dict], *, test_package: bool, external_verified: dict, d_fixed: bool = True,
                   benchmark_specified: bool = False) -> dict:
    """单个(package, capital, permission, execution_profile)配置的16格裁决：先收集全部已确认硬失败，再收集决定性未知。"""
    failures, unknowns = [], []
    by = {(c["start"], c["scenario"]): c for c in cells}
    for sid in START_IDS.values():
        for sc in SCENARIOS:
            if (sid, sc) not in by:
                unknowns.append(f"CELL_MISSING:{sid}/{sc}")
    if not d_fixed:
        unknowns.append("END_DATE_NOT_FIXED")
    for k, ok in external_verified.items():
        if not ok:
            unknowns.append(f"EXTERNAL_NOT_VERIFIED:{k}")
    for c in cells:
        tag = f"{c['start']}/{c['scenario']}"
        st = c.get("startup_status")
        if st == "NOT_STARTED_UNFIT":
            failures.append(f"STARTUP_UNFIT:{tag}")
        elif st != "STARTED":
            unknowns.append(f"STARTUP_UNKNOWN:{tag}:{c.get('startup_reason')}")
            continue
        if c.get("retirement_triggered"):
            failures.append(f"RETIRED_TRIGGERED:{tag}")
        if c.get("MDD_exact") is None:
            unknowns.append(f"MDD_UNKNOWN:{tag}")
        elif c.get("mdd_over_30"):
            failures.append(f"MDD_OVER_30:{tag}")
        if c["scenario"] in ("S0", "S1") and c.get("package_availability") == "FAIL":
            failures.append(f"COVERAGE_AVAILABILITY_FAIL:{tag}")
        elif c["scenario"] in ("S0", "S1") and c.get("package_availability") == "INSUFFICIENT_EVIDENCE":
            unknowns.append(f"COVERAGE_UNASSESSED:{tag}")
        for r in c.get("evidence_reasons", []):
            unknowns.append(f"{r}:{tag}")
        for r in c.get("tax_or_fee_unknown", []):
            unknowns.append(f"{r}:{tag}")
        for r in c.get("decisive_unknowns", []):
            unknowns.append(f"{r}:{tag}")
        if c.get("run_error"):
            unknowns.append(f"RUN_ABORTED:{c['run_error']}:{tag}")
    robust = None
    if not failures and not unknowns:
        vals = []
        for c in cells:
            if c["scenario"] in ("S0", "S1"):
                vals.append(Decimal(c["CAGR_decimal"]) if c.get("CAGR_decimal") is not None else None)
        if any(v is None for v in vals):
            unknowns.append("CAGR_UNAVAILABLE")
        else:
            robust = min(vals)
            if robust <= 0:
                failures.append("ROBUST_CAGR_NOT_POSITIVE")
    if not benchmark_specified:
        unknowns.append("ETF_BENCHMARK_NOT_SPECIFIED")
    if test_package:
        status = "ENGINEERING_ONLY"
    elif failures:
        status = "REJECTED"
    elif unknowns:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "REVIEWABLE"
    fee_rates = [Decimal(c["annual_fee_rate"]) for c in cells if c["scenario"] in ("S0", "S1") and c.get("annual_fee_rate")]
    mdds = [Fraction(int(c["MDD_exact"]["num"]), int(c["MDD_exact"]["den"])) for c in cells if c.get("MDD_exact")]
    return {"status": status, "failures": sorted(set(failures)), "unknowns": sorted(set(unknowns)),
            "robust_cagr": str(robust) if robust is not None else None,
            "worst_mdd": wire_ratio(max(mdds)) if mdds else None,
            "max_annual_fee_rate_s0s1": str(max(fee_rates)) if fee_rates else None}


def rank_configs(results: list[dict]) -> dict:
    """仅对 REVIEWABLE 排序：稳健年化降序、16格最坏MDD升序、S0/S1最高年费用率升序、因子数升序、包ID。"""
    ok = [r for r in results if r["qualification"]["status"] == "REVIEWABLE" and r.get("role") != "B2"]
    if not any(r["qualification"]["status"] in ("REVIEWABLE",) for r in results) and \
            all(r["qualification"]["status"] == "REJECTED" for r in results if r.get("role") != "B0"):
        return {"ranking": [], "conclusion": "NO_ADOPTABLE_CONFIGURATION"}

    def key(r):
        q = r["qualification"]
        w = q["worst_mdd"]
        return (-Decimal(q["robust_cagr"]), Fraction(int(w["num"]), int(w["den"])) if w else Fraction(1),
                Decimal(q["max_annual_fee_rate_s0s1"] or "0"), r.get("factor_count", 0), r.get("package_id") or "")
    ordered = sorted(ok, key=key)
    return {"ranking": [r["config_key"] for r in ordered],
            "conclusion": "RESEARCH_ORDER_ONLY_NOT_AUTHORIZATION" if ordered else "NO_REVIEWABLE_CONFIGURATION"}


def compare_with_b0(b1_a0: dict, b0_a0: dict) -> str:
    """A0的BASE、HIGH中B1年化均不低于B0，且至少一格年化更高、MDD更低或费用负担更低 → INCREMENTAL；
    收益降低而风险改善 → TRADEOFF_PENDING；否则 NO_INCREMENT。"""
    better, worse_ret, better_risk = False, False, False
    for sc in ("S0", "S1"):
        a, b = b1_a0[sc], b0_a0[sc]
        ca, cb = Decimal(a["CAGR_decimal"]), Decimal(b["CAGR_decimal"])
        ma = Fraction(int(a["MDD_exact"]["num"]), int(a["MDD_exact"]["den"]))
        mb = Fraction(int(b["MDD_exact"]["num"]), int(b["MDD_exact"]["den"]))
        fa, fb = Decimal(a["annual_fee_rate"]), Decimal(b["annual_fee_rate"])
        if ca < cb:
            worse_ret = True
        if ca > cb or ma < mb or fa < fb:
            better = True
        if ma < mb or fa < fb:
            better_risk = True
    if worse_ret and better_risk:
        return "TRADEOFF_PENDING"
    if not worse_ret and better:
        return "INCREMENTAL_RECOMMENDATION"
    return "NO_INCREMENT"


def interpret_b0_b1(b0_status: dict, b1_status: dict) -> str:
    """B0 停用而 B1 存续：只记完整方案运行改善，不据此认证纯因子预测能力（D20.2）。"""
    b0_retired = any(f.startswith("RETIRED_TRIGGERED") for f in b0_status.get("failures", []))
    b1_retired = any(f.startswith("RETIRED_TRIGGERED") for f in b1_status.get("failures", []))
    if b0_retired and not b1_retired:
        return "OPERATING_IMPROVEMENT_NOT_PURE_ALPHA"
    return "NO_SPECIAL_INTERPRETATION"


def etf_benchmark_window(listing_date: date | None, start: date, end: date) -> dict:
    """外部 ETF 对象只在其实际上市后的共同窗口内比较；指数长历史不冒充上市前可交易收益。"""
    if listing_date is None:
        return {"status": "BENCHMARK_NOT_SPECIFIED", "window": None}
    if listing_date > end:
        return {"status": "NO_COMMON_WINDOW", "window": None}
    s = max(start, listing_date)
    return {"status": "COMMON_ACTUAL_LIFETIME", "window": (s.isoformat(), end.isoformat()),
            "pre_listing_excluded": listing_date > start}


def config_identity(config: dict) -> str:
    """语义身份由规范化内容确定（规则值、因子公式版本与参数、资金、权限、费用、执行配置），不以文件名/ID 代替。"""
    import json as _json
    ignore = {"package_id", "file_name", "run_id", "study_id", "display_name"}

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in sorted(x.items()) if k not in ignore}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return _json.dumps(strip(config), sort_keys=True, ensure_ascii=False)


def reapplication_check(previous: dict, new_config: dict, *, changes: list, matrix_complete: bool,
                        external_revalidated: bool, user_decision_ref: str | None) -> dict:
    """真实停用后重新申请：须链接旧停用记录、列出实质变化或错误修复、完整复验、外部差异验证与用户新决定。"""
    missing = []
    same = config_identity(previous["config"]) == config_identity(new_config)
    if same:
        missing.append("SAME_CONTENT_RENAMED")
    if not changes:
        missing.append("NO_SUBSTANTIVE_CHANGE_OR_FIX")
    if not matrix_complete:
        missing.append("ORIGINAL_MATRIX_NOT_REPLAYED")
    if not external_revalidated:
        missing.append("EXTERNAL_FACTS_NOT_REVALIDATED")
    if not user_decision_ref:
        missing.append("USER_DECISION_MISSING")
    return {"eligible_for_review": not missing, "missing": missing,
            "previous_retirement": previous.get("retirement_trigger_at"), "history_preserved": True}
