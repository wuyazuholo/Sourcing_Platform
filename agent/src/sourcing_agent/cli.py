"""命令行入口；离线 demo 与真实模型调用明确分开。"""

import argparse
import json
import os
from pathlib import Path

from sourcing_agent.adapters.demo import DemoModel
from sourcing_agent.adapters.models import configured_model
from sourcing_agent.adapters.snapshot import demo_records, publish_snapshot, read_records
from sourcing_agent.bootstrap import create_runner
from sourcing_agent.config import load_environment, selected_provider, selected_snapshot
from sourcing_agent.diagnostics import diagnose
from sourcing_agent.harness.context import ContextPolicy
from sourcing_agent.harness.contracts import RunCanceled, RunLimits
from sourcing_agent.harness.store import RuntimeStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="sourcing-agent", description="有证据边界的供应商调查 Agent"
    )
    commands = root.add_subparsers(dest="command", required=True)
    api = commands.add_parser("api", help="供平台调用的本机 HTTP 调查服务")
    api.add_argument("--env-file", type=Path, required=True)
    api.add_argument("--service-env-file", type=Path, required=True)
    api.add_argument("--port", type=int, default=8090)
    api.add_argument("--state-dir", type=Path, default=Path(".runtime/platform-api"))
    for name in ("demo", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--state-dir", type=Path, default=Path(".runtime/sourcing"))
        sub.add_argument("--namespace", default="local-project")
        sub.add_argument("--session")
        sub.add_argument("--message")
        sub.add_argument("--resume", action="store_true")
        sub.add_argument("--window-tokens", type=int, default=24000)
        sub.add_argument("--max-model-calls", type=int, default=10)
        sub.add_argument("--max-output-tokens", type=int)
        sub.add_argument("--env-file", type=Path)
        sub.add_argument("--engine", choices=("native", "langgraph"), default="native")
        if name == "run":
            sub.add_argument("--snapshot", type=Path)
            sub.add_argument("--provider", choices=("anthropic", "openai"))
            sub.add_argument("--mcp-config", type=Path)
    mcp = commands.add_parser("mcp", help="可选 MCP 工具发现与离线演示")
    mcp_commands = mcp.add_subparsers(dest="mcp_action", required=True)
    mcp_tools = mcp_commands.add_parser("tools")
    mcp_tools.add_argument("--config", type=Path, required=True)
    mcp_tools.add_argument("--env-file", type=Path)
    mcp_demo = mcp_commands.add_parser("demo")
    mcp_demo.add_argument("--state-dir", type=Path, default=Path(".runtime/mcp-demo"))
    mcp_demo.add_argument("--engine", choices=("native", "langgraph"), default="native")
    database = commands.add_parser("database", help="真实 SQL Server 只读查询")
    db_commands = database.add_subparsers(dest="database_action", required=True)
    inspect_db = db_commands.add_parser("inspect")
    inspect_db.add_argument("--env-file", type=Path, required=True)
    inspect_db.add_argument("--report", type=Path)
    search_db = db_commands.add_parser("search")
    search_db.add_argument("--env-file", type=Path, required=True)
    search_db.add_argument("--hs")
    search_db.add_argument("--product")
    search_db.add_argument("--start", required=True)
    search_db.add_argument("--end", required=True)
    search_db.add_argument("--market")
    search_db.add_argument("--supplier-country")
    search_db.add_argument("--exclude-supplier", type=int)
    search_db.add_argument("--limit", type=int, default=5)
    search_db.add_argument(
        "--ranking", choices=("recent", "customer_coverage", "activity"), default="recent"
    )
    search_db.add_argument("--max-records", type=int, default=5000)
    search_db.add_argument("--scan-limit", type=int, default=200000)
    search_db.add_argument(
        "--full-history",
        action="store_true",
        help="显式检索全表历史，仍受查询超时及记录保存上限限制",
    )
    search_db.add_argument("--engine", choices=("native", "langgraph"), default="native")
    search_db.add_argument("--state-dir", type=Path, default=Path(".runtime/database-search"))
    search_db.add_argument("--json", action="store_true")
    search_db.add_argument("--report", type=Path)
    serve_db = db_commands.add_parser("serve")
    serve_db.add_argument("--env-file", type=Path, required=True)
    serve_db.add_argument("--state-dir", type=Path, default=Path(".runtime/database-mcp"))
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--env-file", type=Path)
    doctor.add_argument("--snapshot", type=Path)
    doctor.add_argument("--provider", choices=("anthropic", "openai"))
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--report", type=Path)
    snap = commands.add_parser("snapshot")
    snap.add_argument("--input", type=Path, required=True)
    snap.add_argument("--output", type=Path, required=True)
    snap.add_argument("--scope", required=True)
    memory = commands.add_parser("memory")
    memory.add_argument("action", choices=("set", "list", "delete"))
    memory.add_argument("--state-dir", type=Path, default=Path(".runtime/sourcing"))
    memory.add_argument("--namespace", default="local-project")
    memory.add_argument("--key")
    memory.add_argument("--value")
    memory.add_argument("--source", default="explicit_cli")
    session = commands.add_parser("session")
    session.add_argument("action", choices=("show", "events", "recover"))
    session.add_argument("--state-dir", type=Path, default=Path(".runtime/sourcing"))
    session.add_argument("--namespace", default="local-project")
    session.add_argument("--session", required=True)
    return root


def main() -> None:
    argument_parser = parser()
    args = argument_parser.parse_args()
    try:
        if args.command == "api":
            from sourcing_agent.database_config import database_config
            from sourcing_agent.platform_api import serve_platform

            load_environment(args.env_file)
            load_environment(args.service_env_file)
            serve_platform(
                args.port,
                os.getenv("SOURCING_AGENT_API_TOKEN", ""),
                database_config(),
                args.state_dir,
            )
            return
        if args.command == "database":
            from sourcing_agent.adapters.sqlserver import SqlServerReader
            from sourcing_agent.database_config import database_config

            load_environment(args.env_file)
            if args.database_action == "serve":
                from sourcing_agent.database_mcp import create_database_server

                create_database_server(database_config(), args.state_dir).run()
                return
            if args.database_action == "inspect":
                result = SqlServerReader(database_config()).describe()
            else:
                from sourcing_agent.sourcing.database_search import search_database
                from sourcing_agent.sourcing.models import Requirements

                requirements = Requirements(
                    hs_prefix=args.hs,
                    product_text=args.product,
                    start_date=args.start,
                    end_date=args.end,
                    market_country=args.market,
                    supplier_country=args.supplier_country,
                    exclude_supplier_id=args.exclude_supplier,
                    limit=args.limit,
                    ranking=args.ranking,
                )
                result = search_database(
                    database_config(),
                    requirements,
                    args.state_dir,
                    max_records=args.max_records,
                    scan_limit=0 if args.full_history else args.scan_limit,
                    engine=args.engine,
                )
            text = json.dumps(result, ensure_ascii=False, indent=2)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(text, encoding="utf-8")
            if args.database_action == "search" and not args.json:
                print(result["text"])
                print("\nsnapshot:", result["snapshot"])
                print("state_dir:", result["state_dir"])
                print("session:", result["session_id"])
                print(
                    f"records={result['record_count']} candidates={len(result['candidates'])} scan_limit={result['scan_limit']} truncated={result['result_truncated']}"
                )
            else:
                print(text)
            return
        if args.command == "mcp":
            from sourcing_agent.adapters.mcp import McpGateway
            from sourcing_agent.mcp_config import load_mcp_config

            if args.mcp_action == "tools":
                load_environment(args.env_file)
                gateway = McpGateway(load_mcp_config(args.config))
                print(
                    json.dumps(
                        [d.model_dump(mode="json") for d in gateway.definitions()],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                from sourcing_agent.adapters.mcp_demo import McpDemoModel, demo_config

                snapshot = args.state_dir / "demo-trades.sqlite"
                if not snapshot.exists():
                    publish_snapshot(
                        snapshot, demo_records(), "SYNTHETIC MCP DEMO：虚构交易及企业资料"
                    )
                gateway = McpGateway(demo_config())
                runner = create_runner(
                    snapshot,
                    args.state_dir,
                    "mcp-demo",
                    McpDemoModel(),
                    engine=args.engine,
                    external=gateway,
                )
                session = runner.create_session()
                print("session:", session.id)
                print("engine:", args.engine)
                print("MCP 离线演示：真实 stdio 协议与子进程，模型、交易和企业资料均为虚构样例。")
                result = runner.run(
                    session.id, "找 HS 841891，2025年全年，补查第一家企业的官网资料"
                )
                print(result.text)
                print(
                    f"status={result.status} model_calls={result.model_calls} tool_calls={result.tool_calls}"
                )
            return
        if args.command == "doctor":
            load_environment(args.env_file)
            provider = selected_provider(args.provider)
            snapshot = selected_snapshot(args.snapshot, args.env_file)
            failed = False
            try:
                report = {"ok": True, **diagnose(snapshot, provider, args.live)}
            except Exception as error:
                # SDK 错误不原样打印，防止兼容服务把凭据放入错误响应。
                report = {
                    "ok": False,
                    "error_type": type(error).__name__,
                    "message": "检查失败，请核对数据、模型权限或网络配置",
                }
                failed = True
            text = json.dumps(report, ensure_ascii=False, indent=2)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(text, encoding="utf-8")
            print(text)
            if failed:
                argument_parser.exit(1)
            return
        if args.command == "snapshot":
            print(
                publish_snapshot(args.output, read_records(args.input), args.scope), "条记录已发布"
            )
            return
        if args.command in {"memory", "session"}:
            store = RuntimeStore(args.state_dir / "sourcing.sqlite")
            if args.command == "memory":
                if args.action != "list" and not args.key:
                    argument_parser.error("memory set/delete 需要 --key")
                if args.action == "set":
                    if not args.value:
                        argument_parser.error("memory set 需要 --value")
                    item = store.save_memory(args.namespace, args.key, args.value, args.source)
                    print(item.model_dump_json(indent=2))
                elif args.action == "delete":
                    store.save_memory(args.namespace, args.key, "", args.source, deleted=True)
                    print("已删除当前可召回版本，保留版本轨迹")
                else:
                    print(
                        json.dumps(
                            [m.model_dump(mode="json") for m in store.memories(args.namespace)],
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
            else:
                if args.action == "recover":
                    store.recover_interrupted(args.session, args.namespace)
                    print("已标记中断；可用 run --session ID --resume 显式重跑")
                elif args.action == "events":
                    print(
                        json.dumps(
                            store.events(args.session, args.namespace), ensure_ascii=False, indent=2
                        )
                    )
                else:
                    print(
                        store.load_session(args.session, args.namespace).model_dump_json(indent=2)
                    )
            return
        if args.resume and (not args.session or args.message is not None):
            argument_parser.error("--resume 必须提供 --session，且不能同时提供 --message")
        load_environment(args.env_file)
        if args.command == "demo":
            snapshot = args.state_dir / "demo-trades.sqlite"
            if not snapshot.exists():
                publish_snapshot(
                    snapshot, demo_records(), "SYNTHETIC DEMO：六条虚构交易记录，非真实供应商"
                )
            model = DemoModel()
            message = (
                args.message or "请找 HS 841891，2025 年全年有历史记录的供应商，并比较已知买方数。"
            )
        else:
            provider = selected_provider(args.provider)
            snapshot = selected_snapshot(args.snapshot, args.env_file)
            model, message = configured_model(provider), args.message
            if not args.resume and not message:
                argument_parser.error("run 需要 --message")
        external = None
        if args.command == "run" and args.mcp_config:
            from sourcing_agent.adapters.mcp import McpGateway
            from sourcing_agent.mcp_config import load_mcp_config

            external = McpGateway(load_mcp_config(args.mcp_config))
        runner = create_runner(
            snapshot,
            args.state_dir,
            args.namespace,
            model,
            policy=ContextPolicy(window_tokens=args.window_tokens),
            limits=RunLimits(
                model_calls=args.max_model_calls,
                max_output_tokens=args.max_output_tokens
                or int(os.getenv("SOURCING_MODEL_MAX_OUTPUT_TOKENS", "2048")),
            ),
            engine=args.engine,
            external=external,
        )
        session = (
            runner.store.load_session(args.session, args.namespace)
            if args.session
            else runner.create_session()
        )
        print("session:", session.id)
        print("engine:", args.engine)
        if args.command == "demo":
            print("离线演示：模型决策与交易数据均为受控样例，不代表真实模型效果。")
        result = runner.run(session.id, None if args.resume else message, resume=args.resume)
        print(result.text)
        print(
            f"\nstatus={result.status} model_calls={result.model_calls} "
            f"tool_calls={result.tool_calls} summary_calls={result.summary_calls}"
        )
    except (RunCanceled, KeyboardInterrupt):
        argument_parser.exit(130, "运行已取消；已提交检查点保留。\n")
    except (ValueError, RuntimeError, OSError) as error:
        argument_parser.exit(1, str(error) + "\n")
