# RSI 提示词对照评测

`scripts/rsi_sourcing_eval.py` 是一个受控的 RSI-style 提示词迭代实验：在固定合成数据上，
对照基线 system prompt 与追加执行策略的候选 prompt，按案例通过率与回归决定是否保留
候选提示词。评测不加载真实交易快照，不发送供应商或业务数据到远端。

## 数据与隔离

- 快照由脚本内置的 12 条虚构记录发布到 `.runtime/rsi-evaluation/synthetic-trades.sqlite`，
  字段口径与正式快照一致（HS、日期、买卖方国家、买方 ID 等）。
- 模型来自当前配置的 OpenAI 兼容服务；除固定提示词与合成记录外，不读取 `.runtime/data`
  下的真实快照。
- 每个案例、每次重复使用独立的状态目录与会话，案例之间不共享任务状态。

## 案例划分

案例集位于 `sourcing_tests/fixtures/rsi_cases.json`，split 以 JSON 声明为准，脚本不再
硬编码案例名单。

- **development（12）**：discover、clarify-date、revise-clear-country、compare-buyers、
  compare-recency、buyer-profile、monthly-profile、compare-then-research、rerank、
  no-product、time-outside、unknown-certification。
- **validation（6）**：exclude-reference、invalid-selection、clarify-product、
  unobserved-market、change-product、stale-candidates。

案例覆盖：单轮发现、缺条件追问、改条件与清空条件、按买方数/时效比较、补查买方与月份
活动、比较后补查、重排序、零命中、日期超出覆盖范围、待核实条件、排除企业、越界选择
应追问、切换 HS、陈旧候选失效。

## 断言维度

每一轮按预期条件逐项做布尔检查：

- 终态状态（completed / waiting_user / budget_exhausted 等）；
- 需求字段（HS、日期、国家、市场、排序、排除企业等）；
- 当前候选 ID 列表；
- 是否调用比较 / 补查工具，比较赢家，补查对象；
- 画像内容（买方列表、月份活动）；
- 报告文本包含的关键短语。

## 指标口径

一个案例在一次重复中通过，当且仅当该案例所有轮次的全部检查通过；案例在 `--repeat N`
下通过，当且仅当 N 次重复全部通过。

汇总按 split 分组：

| 指标 | 口径 |
| --- | --- |
| `cases` / `cases_passed` | 案例总数 / 通过案例数 |
| `case_pass_rate` | 通过案例数 ÷ 案例数 |
| `turns` / `turns_passed` | 累计轮次数 / 通过轮次数（跨重复累计） |
| `turn_pass_rate` | 通过轮次数 ÷ 总轮次数 |
| `avg_model_calls_per_case` | 每案例平均模型调用次数 |
| `avg_tool_calls_per_case` | 每案例平均工具调用次数 |
| `avg_elapsed_s` | 每案例平均本地耗时 |
| `repeat_pass_rates` | 每次重复各自的案例通过率列表 |
| `repeat_pass_rate_mean` / `repeat_pass_rate_std` | 重复间通过率均值 / 总体标准差 |
| `flaky_cases` | 重复之间时过时挂、稳定性存疑的案例 |

`--repeat 1` 时标准差记为 0；`flaky_cases` 只列出“部分重复通过、部分重复失败”的案例，
全部通过或全部失败的案例不计入。

## 决策规则

对比基线与候选在 development、validation 两组的结果：

- development 案例通过率提升大于 0；
- validation 案例通过率不降；
- validation 上不存在“基线通过、候选失败”的回归检查项。

三条同时满足记为 `KEEP`，否则 `REJECT`；`--smoke` 只做连通性抽查，决策记为
`SMOKE_ONLY`，不做保留判断。回归项按 `案例:轮次:检查名` 列出。

## 复现

```powershell
# 前置：.env.local 已配置 OpenAI 兼容模型（SOURCING_MODEL_PROVIDER=openai）
python scripts/rsi_sourcing_eval.py --smoke          # 每组一个案例，连通性抽查
python scripts/rsi_sourcing_eval.py --repeat 3       # 每个案例重复 3 次
python scripts/rsi_sourcing_eval.py --cases discover compare-buyers
```

报告写入 `.runtime/rsi-evaluation/report.json`（smoke 为 `smoke-report.json`），包含
两组基线/候选明细、通过率与调用耗时、development 增益、validation 增益、回归项、
决策结论与候选提示词全文。

## 离线自测

评测脚本的分组加载、断言、汇总、回归与决策逻辑由 `sourcing_tests/test_rsi_eval.py`
覆盖，使用脚本化模型与临时合成快照，不需要密钥或远端服务：

```powershell
python -m unittest sourcing_tests.test_rsi_eval
```

## 限制

- 合成数据规模有限，案例通过率只反映这套口径与提示词下的行为，不代表真实市场覆盖
  或真实模型业务准确率。
- 结构化状态断言为主；`answer_contains` 关键词检查依赖模型生成或报告模板文本，
  语义容错有限。
- 重复次数默认为 1，标准差记为 0；重复次数少时方差估计不可靠，正式结论应固定数据、问题集并重复多次。
- 当前只对照两版 system prompt，未覆盖模型选型、温度、工具描述等其他变量。
- 离线脚本化测试验证评测框架本身正确，不替代真实模型上的端到端业务验收。
