# 可选 MCP 外部调查

MCP 客户端将用户明确允许的只读外部工具接到现有供应商调查中。支持 stdio 与
Streamable HTTP，使用官方 Python SDK 2.x；未配置时不加载 MCP SDK，不改变原有调查入口。
当前版本提供通用接入和虚构企业目录演示，尚未连接真实网页搜索或工商信息服务。
另已支持真实 SQL Server 交易查询服务，配置见 [数据库搜索](database-search.md)。

## 安装与本地演示

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[live,graph,mcp,dev]"
.\.venv\Scripts\python.exe -m sourcing_agent mcp demo
.\.venv\Scripts\python.exe -m sourcing_agent mcp demo --engine langgraph
```

演示执行：发现交易候选 → 启动真实 MCP stdio 子进程查询虚构企业 → 提交证据报告。
演示模型、交易和企业资料均为确定性样例，不需要模型密钥、不访问外网。
报告中的 `example.invalid` 不是实际官网，不能当作企业资质或供货证明。
默认产物在 `.runtime/mcp-demo`，可用 `--state-dir` 指定其他位置。

## 配置与工具发现

项目根目录的 `mcp.example.json` 提供 stdio 示例：

```powershell
.\.venv\Scripts\python.exe -m sourcing_agent mcp tools --config mcp.example.json
```

配置必须由使用者审阅，只暴露 `tools` 中明确指定的工具；本地工具名为
`mcp_服务名_工具别名`。工具描述取自本地配置，远端系统指令不会被加载。
`read_only: true` 表示使用者确认该工具仅用于查询，不根据远端注解自动开放工具；
远端明确标记 `readOnlyHint: false` 时拒绝接入。此配置不是对任意服务的沙箱：
接入前仍需确认服务实现及其权限，不能仅凭工具名称判断是否有写操作。

stdio 的 `command` 是可执行文件，`args` 是参数数组，执行时不使用 shell。
`{python}` 指当前 Python；`cwd` 相对配置文件目录解析。进程只继承 SDK 的基础环境，
需要的业务环境变量必须显式映射，例如 `"env": {"SERVICE_TOKEN": "MY_MCP_TOKEN"}`。
不自动把当前模型密钥、完整 `.env` 或整个进程环境交给服务。

HTTP 配置示例（替换为你已授权的实际服务与工具）：

```json
{
  "version": 1,
  "servers": [{
    "name": "company",
    "transport": "streamable-http",
    "url": "https://your-authorized-service.example/mcp",
    "headers_env": {"Authorization": "COMPANY_MCP_AUTH"},
    "timeout_seconds": 30,
    "max_response_chars": 64000,
    "tools": [{
      "name": "lookup_company",
      "alias": "lookup",
      "description": "查询企业公开资料，返回来源网址",
      "read_only": true
    }]
  }]
}
```

`headers_env` 的值是环境变量名称；`COMPANY_MCP_AUTH` 的值应为服务需要的完整请求头值
（例如 `Bearer ...`），不要把密钥写进 JSON。远程要求 HTTPS；本机 localhost 可使用 HTTP。
URL 不接受 userinfo、查询字符串或片段。当前版本支持静态环境变量鉴权，不提供交互 OAuth。
如需本地环境文件，显式使用 `--env-file .env.local`，不自动搜索或加载。

## 与真实模型共同使用

```powershell
.\.venv\Scripts\python.exe -m sourcing_agent run `
  --env-file .env.local --mcp-config mcp.example.json `
  --message "找 HS 841891，2025年全年，并补查第一家企业的公开资料"
```

上述 `run` 会调用配置的真实模型；示例 MCP 服务仍然只返回虚构资料。
连接真实服务后，所选工具参数会发送给该服务；模型依然只接收必要的调查上下文。
换成 `--engine langgraph` 即使用图引擎。正常只查历史候选时无需调用外部工具。

## 业务边界与证据

- 外部工具参数统一包装为 `{"supplier_id": 20, "arguments": {...}}`；
  只允许当前已查询候选的企业 ID。`arguments` 按远端 JSON Schema 在本地校验。
- 外部观察包含服务、工具、检索时间（UTC）、内容 SHA-256、来源网址和原始结果。
  本地摘要先返回模型；原文可用 `read_external_result` 回读，超大结果继续沿用归档及分页。
- 外部观察与交易证据分别存储和渲染，不改写交易数量、买方数或候选排名；来源网页
  与目标企业是否对应、认证是否真实有效，仍须单独核实。服务未给 URL 时明确标记缺失。
- 条件 revision 更新会清空旧候选及旧外部观察。MCP 配置及工具定义摘要绑定会话，
  变更后应创建新会话；同一配置的凭据可通过环境变量轮换。
- 外部调用计入原有工具调用预算。每次协议操作都有超时，目录分页及响应大小有上限，
  每个任务至多保留 20 条外部观察。当前只接收文本、链接及文本资源，不接收图片、音频或二进制。
- 工具级错误以可修正反馈返回模型，不保存为证据；连接失败和超时按现有失败/恢复机制处理。
  不向模型或运行错误打印原始传输异常、凭据请求头或子进程 stderr。

## 连接与恢复语义

当前同步 CLI 在工具发现或每次调用时单独建立连接，并在操作结束或超时后关闭。
stdio 会启动相应子进程；HTTP 适用于可独立查询的服务。此实现不支持依赖长期连接
内部状态的工具工作流。SDK 自动协商协议版本，兼容其支持的旧协议服务。

原生版失败后重跑未提交回合，因此可能再次执行外部只读查询；LangGraph 复用已经
持久化的工具节点。远端调用与本地检查点之间仍不保证 exactly-once，不应用于写操作。
任务状态包含原始外部结果，只在模型输入中发送有界摘要；归档分页规则沿用现有实现。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s sourcing_tests -p test_mcp.py -v
.\.venv\Scripts\python.exe -m unittest discover -s sourcing_tests -v
```

测试使用真实 SDK、本地 stdio 子进程和仅绑定 127.0.0.1 的 HTTP 服务，不访问外部 API。
覆盖允许列表、schema、配置与凭据引用、条件失效、证据留存、超时、错误、跨引擎恢复及 CLI。

参考：[官方 MCP Python SDK 客户端](https://py.sdk.modelcontextprotocol.io/client/)、
[传输配置](https://py.sdk.modelcontextprotocol.io/client/transports/)。
