import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from smallcap.bundle import load_bundle
from smallcap.matrix import matrix_specs
from smallcap.replay import CellRunner
from smallcap.research_market import ResearchMarket
from smallcap.rules import RuleBook
from smallcap.fees import FeeSchedule
pkg, role, cap, year, sc = sys.argv[1:6]
b = load_bundle(ROOT / f"profiles/research_bundles/tushare_research_{pkg}.json")
m = ResearchMarket(ROOT / "data/store_tushare_20261019")
spec = [s for s in matrix_specs(b.study, b.payload("factor"), {"MAIN_SH","MAIN_SZ","CHINEXT","STAR"}, m.financial_codes, [], "p")
        if s.role == role and s.capital == cap and s.start_year == int(year) and s.scenario == sc][0]
r = CellRunner(spec, m, RuleBook(b.payload("rules")), FeeSchedule(b.payload("fees")))
r.run()
L = r.ledger
print("limitations:", dict(list(L.limitations.items())[:20]))
blk = [x for x in L.decision_log if x.get("kind") == "BUYS_BLOCKED"]
print("last BUYS_BLOCKED:", blk[-3:] if blk else None)
