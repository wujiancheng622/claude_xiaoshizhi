# 小市值周频策略｜V5D2详细设计完整包

**基线：概要V5不变。用途：按确定合同编码并独立验收。** 本轮只修订V5D1已定位的边界，未开发生产策略、修改R5/QMT或执行真实报撤单。

## 阅读顺序

| 文件 | 用途 |
|---|---|
| [01 详细设计](01_详细设计_V5D2.md) | 22部分算法和状态；含两级禁买、精确净值及完整订单迁移 |
| [03 接口与输入契约](03_接口与输入契约_V5D2.md) | 唯一Bundle映射、枚举、精确状态、税则日期和重投影 |
| [02 测试设计](02_测试设计与验收用例_V5D2.md) | 原312项＋新增26条关键事件链，共338项生产设计用例，均NOT_RUN |
| [04 校核与外部输入](04_校核与外部输入_V5D2.md) | 实际验证范围、未运行部分及精确外部缺口 |
| [05 逐项复核裁决](05_逐项复核与修订裁决_V5D2.md) | 采纳/部分采纳/不采纳、证据及语义变更分类 |
| [原概要V5](基线_概要设计_V5_20261016.md) | 不变的只读投资基线 |

每份主文均有同名HTML阅读版。`inputs/bundle.schema.json`与两个Bundle模板是唯一研究输入合同；`runtime_contracts.schema.json`仅校验新增的精确状态及税则绑定记录。

`verification/golden_vectors.json`保留原6组并新增26组，共32组固定输入/预期。测试用税则文件明确标为TEST_ONLY，不可用作全部历史税表。因子及真实Profile状态没有被自动升级。

## 离线小型复验

```bash
python verification/verify_design.py
python verification/verify_revision.py
python verification/check_documents.py
```

第一个脚本仅需Python标准库；第二、三个另使用已安装的jsonschema，文档检查还需beautifulsoup4和markdown-it-py。脚本不安装依赖、不联网、不读取凭据或交易。阅读HTML无需这些依赖。

结果分别保存为`verification_results.json`、`revision_results.json`、`document_checks.json`。原局部278项与新增26条参考链不是338项生产验收；后者必须由真实实现独立执行。96格市场回放及真实QMT/券商/共享账户测试仍未运行。

## 给编码模型的约束

以01、03、Schema的同版合同实现；以02和黄金向量建立自己的测试。不能为通过测试改动投资参数，不接受旧DAILY_PROXY别名或两套Study格式，不把50位Decimal近似作为硬净值门槛。旧D1近似快照只能从可信事件重建，不能直接升级。

FORWARD_MANUAL中的报撤单动作仍为人工清单和导入确认。本包不授权增加真实自动下单/撤单，也不要求建设通用协调平台。
