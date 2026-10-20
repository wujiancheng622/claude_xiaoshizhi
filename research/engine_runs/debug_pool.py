import sys
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
        if s.role == "B1" and s.capital == "500000" and s.start_year == 2024 and s.scenario == "S0"][0]
r = CellRunner(spec, m, RuleBook(b.payload("rules")), FeeSchedule(b.payload("fees")))
d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2024, 1, 5)
pool = r.pool_for(d, sh_dt(d, time(20)))
print(d, pool["health"], pool["size"], "uncertain", len(pool["uncertain"]), "irrelevant", len(pool["irrelevant_unknown"]))
c = Counter()
for sid in pool["uncertain"]:
    c.update(pool["exclusion_reasons"][sid])
print(c.most_common())
for sid in pool["uncertain"][:8]:
    print(sid, pool["exclusion_reasons"][sid], m.master[sid]["board"], m.industry(sid, d), m.bar(sid, d))
