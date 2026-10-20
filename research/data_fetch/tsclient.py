"""只读 Tushare 客户端：复用 ETF 仓库 etf_data_fetcher 的代理配置与限速（不打印、不保存任何凭据）。"""
from __future__ import annotations

import sys
from pathlib import Path

FETCHER = Path("/home/wjc/src/github/etf/etf_data_fetcher")
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)
PRIOR_RAW = Path("/home/wjc/src/github/etf/artifacts/stock_multifactor_20261015/raw")   # 2026-10-15 已抓数据（只读复用）
sys.path.insert(0, str(FETCHER))

from tushare_runtime import TushareClientFactory, load_tushare_runtime_settings  # noqa: E402


def factory(delay: float | None = None) -> TushareClientFactory:
    s = load_tushare_runtime_settings(FETCHER / "config.yaml")
    if delay is not None:
        from dataclasses import replace
        s = replace(s, request_delay=delay)
    return TushareClientFactory(s)
