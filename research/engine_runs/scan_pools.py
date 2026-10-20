import sys, time as _t
from collections import Counter
from datetime import date, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from smallcap.bundle import load_bundle
from smallcap.contracts import sh_dt
from smallcap.matrix import matrix_specs
from smallcap.replay import CellRunner
from smallcap.research_market import ResearchMarket
from smallcap.rules import RuleBook
from smallcap.fees import FeeSchedule
b = load_bundle(ROOT / "profiles/research_bundles/tushare_research_P1_SIZE_EP_VOL.json")
m = ResearchMarket(ROOT / "data/store_tushare_20261019")
spec = [s for s in matrix_specs(b.study, b.payload("factor"), {"MAIN_SH", "MAIN_SZ", "CHINEXT", "STAR"}, m.financial_codes, [], "p")
        if s.role == "B1" and s.capital == "500000" and s.start_year == 2011 and s.scenario == "S0"][0]
r = CellRunner(spec, m, RuleBook(b.payload("rules")), FeeSchedule(b.payload("fees")))
weeks = [d for d in m.calendar.week_ends(m.data_end) if d >= date(2010, 12, 31)]
t0 = _t.time(); bad = Counter(); reasons = Counter(); cov = []
for d in weeks:
    pool = r.pool_for(d, sh_dt(d, time(20)))
    if pool["health"] != "OK":
        bad[d.year] += 1
        for sid in pool["uncertain"]:
            reasons.update(pool["exclusion_reasons"][sid])
    else:
        fv = r.factor_values(d, sh_dt(d, time(20)), pool)
        from smallcap.factors import score_package
        sc = score_package(spec.package, pool, fv, pool["codes"])
        cov.append((d, float(sc["coverage"]), sc["healthy"]))
print(f"{len(weeks)} weeks in {_t.time()-t0:.0f}s; unhealthy pools by year:", dict(bad), "reasons:", reasons.most_common(6))
low = [(str(d), round(c, 3)) for d, c, h in cov if not h]
print("coverage<80% weeks:", len(low), low[:10])
print("coverage min/median:", min(c for _, c, _ in cov), sorted(c for _, c, _ in cov)[len(cov)//2])
