# 真实 SQL Server 供应商搜索

`database search` 从 XpressfeedDB 的 `dbo.panjivaMXExport` 读取真实墨西哥出口交易，冻结为本地
SQLite 快照，再复用现有候选排序、证据核验和报告流程。支持 native / langgraph。
此入口采用确定性流程，不调用外部模型，不消耗模型 API 预算。自然语言调查仍使用原有 `run` 入口。

## 本机配置与安装

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[database,mcp,graph]"
# 首次配置时复制模板；已有本机配置请直接使用，避免覆盖。
Copy-Item .env.database.example .env.database.local
```

在 `.env.database.local` 填写 `SOURCING_DB_HOST`、`SOURCING_DB_USER` 和
`SOURCING_DB_PASSWORD`。数据库默认 XpressfeedDB，端口默认 1433；文件已被 Git 忽略。
程序只显式加载 `--env-file` 指定文件，进程中已有同名变量优先。不要提交或分享该文件。
模板默认要求加密并验证服务器证书；自签名证书的既有环境可显式配置
`SOURCING_DB_TRUST_SERVER_CERTIFICATE=yes`。登录超时默认 5 秒，查询超时默认 15 秒。
驱动为 pymssql / FreeTDS，无需安装系统 ODBC 驱动。

## 查询与产物

```powershell
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m sourcing_agent database inspect --env-file .env.database.local
.\.venv\Scripts\python.exe -m sourcing_agent database search `
  --env-file .env.database.local --hs 8544 `
  --start 2025-01-01 --end 2025-12-31
```

日期包含起止两天。可用 `--product` 搜索商品描述中的字面文本，支持同时指定 HS；
`--supplier-country`、`--exclude-supplier`、`--limit`、`--ranking` 用于调整筛选和排序。
国家按源库英文值填写。`--engine langgraph` 选择图引擎，`--json` 输出结构化结果，
`--report 路径` 额外保存报告。

每次查询在 `.runtime/database-search/<唯一ID>/` 保存 `trades.sqlite`、`report.json`
及会话/事件。报告记录源表、查询条件、UTC 提取时间、扫描窗口、截断状态和快照 SHA-256；
引用保留源记录 ID，便于复查。零匹配会保存空快照并明确报告没有候选。
这些本地运行产物含业务数据，均由现有 `.runtime` 忽略规则排除。

## 查询范围与性能

当前墨西哥源表的条件查询缺少已确认的搜索索引。因此默认先按主键倒序取最近至多
**20 万条源记录**，再按商品、日期、国家筛选。主键窗口不是日期窗口；默认查询无法覆盖
该表的全部历史，也不代表完整市场。报告会明确显示这一范围。

`--scan-limit` 可调整为 1—2000000；`--full-history` 或 `--scan-limit 0` 显式请求全历史条件查询。
全历史仍受超时约束，可能超时失败。需要频繁查询时，应由数据库管理员评估索引或专用查询副本。
程序不会在服务器创建索引或修改表。

默认至多保存 **5000 条匹配记录**，`--max-records` 可调整为 1—50000。
超过上限时 `result_truncated=true`，报告提示排名与统计只覆盖保存部分；应缩小查询条件。
未截断也仅能证明在声明的源库窗口内检索完成。

数据库 SQL 使用固定表、固定字段映射和参数化条件，不接收任意 SQL；连接声明只读意图，
正常关闭时回滚。只读意图不能代替数据库权限，建议账号仅授予所需 SELECT 权限。

## 通过 MCP 调用

```powershell
.\.venv\Scripts\python.exe -m sourcing_agent database serve --env-file .env.database.local
# 另一个终端可验证允许列表中的工具发现：
.\.venv\Scripts\python.exe -m sourcing_agent mcp tools --config mcp.database.example.json
```

`serve` 启动本地 stdio MCP 服务，提供 `search_trade_suppliers`。
调用参数示例：

```json
{"hs_prefix":"8544","start_date":"2025-01-01","end_date":"2025-12-31","limit":5,"scan_limit":200000,"max_records":5000}
```

MCP 返回同样的快照、候选、交易证据和范围元数据。服务进程自行读取本机环境文件；
客户端配置只保存路径，不包含账号密码。服务会写本地运行产物，不写源数据库。
默认客户端超时 30 秒；若显式提高数据库查询超时，应同步调整客户端超时。

`mcp.database.example.json` 可用于外部 MCP 客户端配置参考。本项目已有 `run --mcp-config`
用于补查当前候选，返回数据作为独立外部观察，不自动替换当前任务的交易快照。
首次从源库发现供应商请使用 `database search` 或直接调用此 MCP 工具；不要把补查结果
当作已合并进旧快照的交易事实。网页搜索和工商信息查询仍需另行接入实际服务。
