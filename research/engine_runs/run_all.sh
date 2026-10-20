#!/usr/bin/env bash
# 在研究数据仓上用生产引擎跑 4 个预登记候选包的 V5D2 96 格矩阵（P1 含 B0；其余只跑 B1/B2）。
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
STORE=data/store_tushare_20261019
for P in P1_SIZE_EP_VOL P2_SIZE_EP_TURN P3_EP_VOL_TURN P4_SIZE_EP_VOL_TURN P5_SIZE_EP_POSTHOC; do
  ROLES="B1,B2"; [ "$P" = "P1_SIZE_EP_VOL" ] && ROLES="B0,B1,B2"
  echo "== $P ($ROLES) $(date +%T)"
  /usr/bin/time -v $PY -m smallcap replay --bundle profiles/research_bundles/tushare_research_$P.json \
      --out runs/research_$P --research-store $STORE --workers 12 --roles $ROLES > runs/research_$P.log 2> runs/research_$P.time
  grep -E "Elapsed|Maximum resident" runs/research_$P.time
done
echo "DONE $(date +%T)"
