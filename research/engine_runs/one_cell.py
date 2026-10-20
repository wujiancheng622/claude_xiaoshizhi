"""在研究数据仓上用生产引擎跑单个单元（调试/计时用）。用法：python one_cell.py BUNDLE ROLE CAPITAL YEAR SCEN"""
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from smallcap.bundle import load_bundle  # noqa: E402
from smallcap.matrix import matrix_specs, run_cell_record  # noqa: E402
from smallcap.research_market import ResearchMarket  # noqa: E402

b = load_bundle(sys.argv[1])
role, cap, year, sc = sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
m = ResearchMarket(ROOT / "data" / "store_tushare_20261019")
uni = b.payload("universe")
allowed = {p["board"] for p in uni["permissions"] if p["status"] == "ALLOWED"}
specs = [s for s in matrix_specs(b.study, b.payload("factor"), allowed, m.financial_codes, [], "perm-main")
         if s.role == role and s.capital == cap and s.start_year == year and s.scenario == sc]
t0 = time.time()
rec = run_cell_record(b, specs[0], m)
print(f"{time.time()-t0:.1f}s")
for k in ("cell_key", "startup_status", "startup_reason", "run_error", "retirement_triggered", "trigger_at", "CAGR_decimal",
          "MDD_display", "fills", "fee_total", "annual_fee_rate", "average_names", "package_availability",
          "healthy_week_ratio", "frozen_exit_ratio_max", "tax_or_fee_unknown", "decisive_unknowns", "first_execution"):
    print(k, rec.get(k))
