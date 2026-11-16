# Agent 分支：安装与启动

本分支包含完整 demo 所需的前后端及 Python Agent 源码，不依赖相邻的其他仓库目录。
`frontend/` 为 Vue 页面，`backend/` 为 Spring Boot 鉴权与代理接口，`agent/` 为 Python 调查服务。
当前真实数据源统一为 SQL Server 的 `XpressfeedDB.dbo.panjivaMXExport` 墨西哥出口表。

## 环境要求

- Python 3.12 或以上。
- Java 21，设置 `JAVA_HOME`；Maven 3.9 可从 PATH 调用。
- Node.js 24 LTS，包含 npm。
- 查询真实数据时，需要能够访问 SQL Server，并具备所需表的 SELECT 权限。

工具链、依赖目录、账号密码、数据库快照和运行日志均不在仓库中。

## Windows：首次安装

在项目根目录打开 PowerShell：

```powershell
git switch agent
python -m venv agent/.venv
Push-Location agent
.\.venv\Scripts\python.exe -m pip install -e ".[database,mcp,graph,live,dev]"
Pop-Location

Copy-Item agent/.env.database.example agent/.env.database.local
```

在本机 `agent/.env.database.local` 填写数据库主机、账号和密码。已有该文件时直接编辑，
不要重新复制覆盖。模板默认要求加密及证书验证；具体证书设置应与可用的数据库环境一致。
此文件被 Git 忽略，不要提交密码。当前页面查询采用本地确定性流程，不需要模型 API Key。

## 启动与访问

```powershell
$env:PYTHONUTF8 = "1"
.\scripts\start-agent-platform.ps1
```

首次启动时，脚本会构建后端，并通过 `npm ci` 安装前端锁定的依赖。默认地址为
`http://127.0.0.1:5173/#/agent`。先注册并登录，再点击左侧“Agent 调查”。本地开发短信
验证码固定为 `123456`，不发送真实短信。

脚本默认使用 `local,embedded` 开发配置，登录账号保存到 `backend/data/platform-auth.mv.db`。
业务交易来自真实 SQL Server，开发账号库不保存源库交易。要使用原有 MySQL 账号库，先配置
MySQL，再运行 `start-agent-platform.ps1 -AuthStore mysql`；两种账号库互相独立。
应用的默认数据库配置仍为 MySQL，embedded 仅由此启动脚本显式启用。

首次启动会生成匹配的服务令牌，保存在 `agent/.env.platform.local` 和
`backend/data/agent-local.properties`；不会打印或覆盖已有凭据。不一致时停止并提示。
若平台没有业务库配置，会从 Agent 的本机数据库配置准备 `vendor-local.properties`，
供原有墨西哥贸易线索页使用；已有配置保留。

```powershell
# 源码更新后重新构建后端
.\scripts\start-agent-platform.ps1 -Build
# 使用另一个 Agent 目录
.\scripts\start-agent-platform.ps1 -AgentRoot 'C:\your-path\sourcing-agent'
# 停止本脚本启动的本机服务
.\scripts\stop-agent-platform.ps1
```

三个端口默认为 8090 / 8080 / 5173，均绑定本机。端口已占用时不会另起冲突服务。
日志位于 `.local/`，错误日志文件名带 `-error`。启动脚本可以读取本机
`.local/toolchain.json` 中的 `jdk`、`maven` 绝对路径；该文件不应提交。

## 调查流程与数据边界

输入 HS 或源语言商品描述、起止日期及可选目的国，提交异步调查。默认示例为 HS 8544、2025 年。
可查看候选入选依据、月份活动、主要已知买方、HS 分布和交易引用；可选择 2—5 家按指标对比，
也可暂不考虑并还原候选。画像与对比都使用同一冻结快照，不重新查询源库。

当前 demo 默认先按记录编号倒序取最近至多 20 万条源记录再筛选，最多保存 5000 条匹配。
超过上限会提示截断，统计和排序只覆盖已保存部分。这不是全表或完整市场覆盖；本分支没有
做搜索覆盖扩展或源库索引改造。完整覆盖仍是后续目标，全历史选项也受超时约束。

日期和货值分别来自 `shpmtDate`、`valueOfGoodsItemCIFUSD`。已知买方按 ID 统计，缺失标识
单独说明，不把缺失当作没有买方；记录数不等于票数。历史交易不证明当前供货、产能或资质。
中文商品词建议是小型手工演示词表，只编辑草稿，不自动翻译、修改 HS 或重新提交。
旧任务没有详细画像时仍可查看报告与原始引用，不用少量证据推算全量统计。

Vue 通过现有登录会话及 CSRF 校验访问 Spring Boot，后端用服务令牌访问 Python。
owner 来自登录主体；用户只能读取自己的任务。浏览器不持有数据库密码或服务令牌。
任务状态持久化在 `agent/.runtime/platform-api`；刷新或返回页面可以恢复结果，
不会自动创建第二次查询。页面临时选择仅存于标签页内存。

## 无数据库的离线演示与测试

```powershell
Push-Location agent
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m sourcing_agent demo
.\.venv\Scripts\python.exe -m sourcing_agent mcp demo
.\.venv\Scripts\python.exe -m unittest discover -s sourcing_tests -v
Pop-Location

Push-Location frontend
npm ci
npm run build
node --test tests/*.test.mjs
Pop-Location

Push-Location backend
mvn '-Dtest=Mexico*,Agent*' test
Pop-Location
```

离线 demo 的交易和企业资料为虚构样例。Agent 单元测试使用模拟数据库和本机 MCP 传输，
不要求真实 SQL Server 或模型密钥。旧 `AuthIntegrationTest` 需要独立 MySQL 测试库，
不能对个人或业务账号库直接执行。此前本地验证包含 156 项 Agent、23 项后端和
16 项前端测试，以及隔离账号库下的真实浏览器验收。

## MCP 与 RSI

数据库 MCP 服务与示例配置见 [数据库搜索](../agent/docs/database-search.md)、
[MCP 接入](../agent/docs/mcp.md)。在 `agent/` 内运行相关命令。

RSI 保留为 `agent/scripts/rsi_sourcing_eval.py` 的受控提示词对照评测，使用合成数据；
实际运行模型对照评测需自行配置本机模型凭据，可能产生 API 费用。
执行方法见 [RSI 评测](../agent/docs/rsi-evaluation.md)。当前页面没有 RSI 评测中心或策略发布功能。

Python Agent 保留原 MIT 许可和版权声明，见 [Agent LICENSE](../agent/LICENSE)。
