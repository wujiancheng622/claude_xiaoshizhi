# 小市值周频策略 V5D2 实现（受限研究回放 + 人工前向）

以《小市值周频策略_V5D2_完整施工交接包_20261017》（解压在 `handoff/`）为唯一实施合同：概要 V5 为投资边界，`01` 详细设计、`03` 接口契约、`inputs/*.schema.json` 为实现依据，`02` 测试设计 G01—G19（338 条）为验收清单。

**安全边界（硬性）**
- 不访问任何券商买卖接口，不提交、不撤销真实订单；没有 `send-live-orders` 命令，`LIVE_ORDER` 一律拒绝。
- `FORWARD_MANUAL` 只做三件事：生成人工操作清单（预约委托，状态 RESERVED）、导入人工确认与券商回报、对账/恢复。
- 不读 `.env`、token、账户号；不联网。共享现金协调是人工串行台账的最小实现，**未经外部演练验收，不能用于共享实盘**。
- 所有 `TEST_ONLY` 夹具、合成市场和示例 Bundle 都不是真实事实，不得升级为 `VERIFIED_FOR_SCOPE`。

## 环境

- Python 3.12（标准库）+ `jsonschema`（本机已有 4.10.3）。无其他依赖，无需安装。
- 在本目录运行：`python3 -m smallcap ...`

## 目录

```text
smallcap/            生产代码（单进程、本地文件）
  contracts.py       固定常量、原因码、Result、精确有理数/金额/时间
  clock.py           市场日历、周度截止、执行窗口、压力时钟
  data.py            来源标准化、QMT优先/Tushare补充裁决、as-of、财报衍生量、修复影响范围
  bundle.py          唯一输入 Bundle：基线常量校验 → Schema → StudyDefinition 派生 → 区间唯一匹配
  rules.py fees.py   RuleBook（数量网格/价格档/涨跌停/清算）；委托级累计计费与股息税制（按登记日选择）
  pool.py factors.py 经济400池（三值资格）；因子包登记、联合有效集、精确平均并列百分位、缺失计数、包级可用性
  portfolio.py       周度意图：30/45缓冲、固定金额、FLAT_SCORE、槽位与行业规划
  risk.py            风险段、两级禁买唯一裁决、合并卖量（比例减仓+费用闭合）、D09.4 在途单取消
  orders.py          D09.3 委托状态机（不可变成交→终态→撤销请求→受理）
  execution.py       保护限价、报价新鲜度、容量、费用后买入预览与数量、首次资金适配、重试建议
  account.py         唯一股数现金账本（事件投影、精确份额、税批次、公司行动、残余、快照、晚到重投影）
  journal.py         单写者 JSONL 事务日志（fsync）、快照、截断尾行与恢复
  shared_cash.py     两策略人工串行批次、双现金门、原子转资、对账
  replay.py minute.py 日线09:30代理 / 分钟10:00—10:30 因果撮合；可在任一已提交事件后崩溃并恢复
  evaluation.py matrix.py 指标、资格（先判后排）、96格矩阵调度（不删格）
  forward.py snapshot.py FORWARD_MANUAL 会话、人工清单、回报导入、对账、恢复；只读事实快照适配
  synthetic.py       TEST_ONLY 合成市场（虚构证券，仅工程联调）
  schema/            Bundle 与运行时精确状态 Schema（原样复制自交接包）
tests/               G01—G19 生产测试（unittest），用例编号以 @case 标注；test_review_fixes.py 为 2026-10-19 复查修复的回归测试
tools/run_acceptance.py      运行全部测试并把结果逐项映射到 338 条用例（PASS/FAIL/NOT_RUN/BLOCKED）
tools/make_engineering_bundle.py  生成 TEST_ONLY 工程 Bundle（合成市场专用）
reports/             验收结果、矩阵汇总与交付报告（生成）
runs/                回放输出：cells.jsonl、qualification.json、config_identities.json、summary.md，
                     以及逐格 cells/<cell_key>/ledger.csv、trades.csv、plans.jsonl（--persist-events 时另有 events.jsonl.gz）
```

## 常用命令

```bash
# 1) 校验输入（结构通过 ≠ 来源已核验；会列出 NOT_PROVIDED / TEST_ONLY / 未核验项）
python3 -m smallcap validate-inputs --bundle handoff/小市值周频策略_V5D2/inputs/external_inputs.template.json

# 1b) 只读来源裁决：把已导出的 QMT/Tushare 行（每行一个字段值）按 SourcesProfile 映射规范化，
#     每(证券,字段,期间)一次裁决（QMT优先/同口径Tushare替代/冲突不掩盖），输出 observations.jsonl 与 facts.jsonl（来源轨迹）
python3 -m smallcap build-facts --bundle B.json --raw rows.jsonl --out facts/

# 2) 运行验收：全部测试 + 338 条用例状态，输出 reports/acceptance_results.json 与 acceptance_summary.md（约2分钟）
python3 tools/run_acceptance.py

# 3) 真实输入的 96 格矩阵（外部资料未补齐时，96 格全部输出 NOT_STARTED_UNKNOWN 与阻断原因，不删格）
python3 -m smallcap replay --bundle handoff/小市值周频策略_V5D2/inputs/external_inputs.template.json --out runs/real_template

# 4) 工程联调：TEST_ONLY 合成市场上的 96 格（只验证引擎/调度，不是投资证据）
python3 tools/make_engineering_bundle.py
python3 -m smallcap replay --bundle examples/engineering_synthetic_bundle.json --out runs/engineering_synthetic_daily --synthetic-engineering --workers 12

# 5) 人工前向（需要 execution_mode=FORWARD_MANUAL 的 Bundle、只读事实快照 JSON、账户日志目录）
python3 -m smallcap plan --bundle B.json --journal runs/acct --snapshot facts.json --date 2026-10-16 --phase CUTOFF
python3 -m smallcap plan --bundle B.json --journal runs/acct --snapshot facts.json --date 2026-10-19 --phase EXEC
#    → runs/acct/plans/checklist-2026-10-19.json：人工核对后在券商终端手工操作
#    → runs/acct/reports/ledger.csv、trades.csv、plans.jsonl：人工可读日账与周计划
python3 -m smallcap import-broker-events --bundle B.json --journal runs/acct --file confirmations.csv [--status-map map.json]
python3 -m smallcap reconcile --bundle B.json --journal runs/acct --broker broker_snapshot.json
python3 -m smallcap resume --bundle B.json --journal runs/acct

# 6) 资格复核（带 Bundle 时按各 Profile 的 VERIFIED_FOR_SCOPE 与适用区间判断外部事实）
python3 -m smallcap evaluate --cells runs/X/cells.jsonl --bundle B.json
```

前向相位顺序：首日 `INIT`（入金、首个计划与首次资金适配）→ 每个交易日 `PREOPEN`、`EXEC`、`CLOSE` → 周度最后交易日 `CUTOFF`。同一日期同一相位只执行一次，重跑返回 `ALREADY_DONE`。`EXEC` 的清单含 `SUBMIT_ORDER`（预约，状态 RESERVED）与 `REQUEST_CANCEL`（D09.4 需人工撤的旧委托），另附只读的 `final_intents`（H06.2）。

已有持仓账户（Bootstrap `mode=EXISTING_MANUAL_ACCOUNT`）：先用 JSON 行导入人工核对的 `BOOTSTRAP_ACCOUNT`（现金、持仓、估值、取得日；接入前须了结全部未结委托），再执行 `INIT`；不入金、不做全现金首次适配。`retirement_history_checked=false` 时前向一律阻断。

`import-broker-events` 接受 JSON 数组或 CSV，`kind` 取值：
- 券商确认：`ACK`、`FILL`（必须带券商稳定成交编号 `trade_id`）、`TERMINAL`（`status`/`cum_qty`/`cum_value`/`source_revision`）、`CANCEL_FAILED`；也可给 `ORDER_STATUS` 行并用 `--status-map` 把券商状态值映射为标准状态（映射须有实际 SDK 版本样例；未映射的状态记冲突、不释放预约）。
- 人工确认：`MANUAL_SUBMITTED`、`MANUAL_NOT_SUBMITTED`、`MANUAL_CANCEL_REQUESTED`。
- 人工核对的账户事实（都要 `evidence_ref`）：`CAPITAL_FLOW`（真实出入金）、`CASH_RESTRICTION`、`SHARE_FREEZE`、`BOOTSTRAP_ACCOUNT`、`ORDER_RESOLVE`、`LIMITATION_CLEAR`。

每行先在账本副本上校验，通过后才写日志并入账；被拒的行不落盘（保证日志始终可重放），改以 `EVENT_CONFLICT` 限制事件记录并停止新增风险。同一成交编号同内容重复导入是幂等的。对账差异与解除同样以事件写入日志，恢复后仍在。

同一账户日志绑定一份规范化的运行身份（`RUN_BINDING`：单元定义、因子包、权限、ETF 范围、RuleBook 与费用表内容）。用不同内容的 Bundle 续跑会报 `STUDY_DEFINITION_MISMATCH`；费用/规则内容需要修订时，须走明确的历史纠错并另起日志（本版未实现在同一日志内的规则改版）。

## 输入

- 唯一外部输入是 `bundle.json`（`smallcap/schema/bundle.schema.json`）。研究常量必须与 V5D2 基线逐项相等，例如把 60% 改成 70% 会直接报 `BASELINE_MISMATCH`。
- 前向和回放使用的市场事实，要通过符合 `snapshot.py` 中 `SMALLCAP_SNAPSHOT` 格式的只读快照提供：日历、证券主表、未复权日线（含停牌状态、参考前收、总市值）、历史申万一级行业、风险状态（含公开时刻）、因子值、公司行动、金融业分类代码。本仓库不负责从 QMT/Tushare 取数，导出脚本需要另外按 `03` 的字段映射核验后编写。
- RuleBook：同一市场/板块/日期只有一套规则时直接使用；有多套时按证券当时的风险状态选择，`security_scope_ref` 须为 `NORMAL`、`RISK_WARNING`（ST/*ST）或 `DELISTING_ARRANGEMENT`，状态未知不猜。
- 因子：生产代码只登记了 `TEST_ONLY_VALUE_FROM_FIXTURE`。最终投资因子要在 `factors.REGISTRY` 中按 `03 H03` 增加已登记的纯函数，带公式、边界和手算例；不允许 `eval` 配置表达式。
