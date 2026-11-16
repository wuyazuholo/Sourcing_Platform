"""Real local MCP transports and business integration; no remote services or keys."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_sourcing_agent import ScriptedModel, actions

from sourcing_agent.adapters.mcp import McpGateway, _nested_schema
from sourcing_agent.adapters.mcp_demo import McpDemoModel, demo_config
from sourcing_agent.adapters.snapshot import demo_records, publish_snapshot
from sourcing_agent.adapters.trade import TradeRepository
from sourcing_agent.bootstrap import create_runner
from sourcing_agent.harness.context import validate_tool_pairs
from sourcing_agent.harness.contracts import RunLimits, ToolInputError
from sourcing_agent.harness.store import encode
from sourcing_agent.mcp_config import McpConfig, load_mcp_config
from sourcing_agent.sourcing.research import ResearchTask

HAS_MCP = bool(importlib.util.find_spec("mcp") and importlib.util.find_spec("jsonschema"))
ENGINES = ["native"] + (["langgraph"] if importlib.util.find_spec("langgraph") else [])
ROOT = Path(__file__).resolve().parents[1]
QUESTION = "找 HS 841891，2025年全年，补查第一家企业的官网"


class McpConfigTests(unittest.TestCase):
    def test_config_resolves_python_and_cwd(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(
                (ROOT / "mcp.example.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            config = load_mcp_config(path)
            self.assertEqual(config.servers[0].command, sys.executable)
            self.assertEqual(config.servers[0].cwd, path.parent.resolve())

    def test_config_errors_do_not_echo_pasted_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text('{"password":"TEST-SECRET-NEVER-ECHO"}', encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_mcp_config(path)
            self.assertNotIn("TEST-SECRET", str(caught.exception))

    def test_rejects_unsafe_urls_writes_and_duplicates(self):
        base = {
            "name": "web",
            "transport": "streamable-http",
            "url": "https://example.invalid/mcp",
            "tools": [
                {"name": "lookup", "alias": "lookup", "description": "Lookup", "read_only": True}
            ],
        }
        for url in [
            "file:///key",
            "http://remote.invalid/mcp",
            "https://key@example.invalid/mcp",
            "https://example.invalid/mcp?token=KEY",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                McpConfig.model_validate({"servers": [{**base, "url": url}]})
        with self.assertRaises(ValueError):
            McpConfig.model_validate(
                {"servers": [{**base, "tools": [{**base["tools"][0], "read_only": False}]}]}
            )
        with self.assertRaises(ValueError):
            McpConfig.model_validate({"servers": [base, base]})

    def test_base_import_does_not_load_optional_sdk(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import sys; import sourcing_agent.bootstrap; assert 'mcp' not in sys.modules; assert 'jsonschema' not in sys.modules",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(HAS_MCP, "optional MCP dependencies not installed")
class McpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = McpGateway(demo_config())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "trades.sqlite"
        publish_snapshot(self.snapshot, demo_records(), "SYNTHETIC MCP tests")
        self.repo = TradeRepository(self.snapshot)

    def task(self):
        task = ResearchTask(self.repo, {}, QUESTION, self.gateway)
        task.execute("find_supplier_candidates", {})
        return task

    def test_allowlist_and_argument_validation_prevent_remote_calls(self):
        self.assertEqual([d.name for d in self.gateway.definitions()], ["mcp_demo_lookup"])
        with patch.object(self.gateway, "_request") as request:
            for name, arguments in [
                ("mcp_demo_lookup", {}),
                ("mcp_demo_lookup", {"company_name": 123}),
                ("disabled_write_demo", {"value": "x"}),
            ]:
                with self.assertRaises(ToolInputError):
                    self.gateway.call(name, arguments, 20)
            request.assert_not_called()

    def test_rejects_write_annotations_and_remote_schema_refs(self):
        catalog = [
            {
                "name": "lookup_company",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": False},
            }
        ]
        with (
            patch.object(McpGateway, "_request", return_value=catalog),
            self.assertRaises(ValueError),
        ):
            McpGateway(demo_config())
        catalog[0]["annotations"] = {"readOnlyHint": True}
        catalog[0]["inputSchema"]["properties"] = {"x": {"$ref": "https://example.invalid/schema"}}
        with (
            patch.object(McpGateway, "_request", return_value=catalog),
            self.assertRaises(ValueError),
        ):
            McpGateway(demo_config())

    def test_wrapped_schema_preserves_local_refs(self):
        from jsonschema import Draft202012Validator

        schema = {
            "type": "object",
            "$defs": {"Q": {"type": "string"}},
            "properties": {"q": {"$ref": "#/$defs/Q"}},
        }
        wrapper = {"type": "object", "properties": {"arguments": _nested_schema(schema)}}
        Draft202012Validator(wrapper).validate({"arguments": {"q": "supplier"}})
        self.assertEqual(schema["properties"]["q"]["$ref"], "#/$defs/Q")

    def test_changed_remote_definition_is_rejected_before_execution(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from mcp.types import Tool, ToolAnnotations

        class ChangedClient:
            call_tool = AsyncMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def list_tools(self, **kwargs):
                return SimpleNamespace(
                    tools=[
                        Tool(
                            name="lookup_company",
                            input_schema={
                                "type": "object",
                                "properties": {"changed": {"type": "boolean"}},
                            },
                            annotations=ToolAnnotations(read_only_hint=True),
                        )
                    ],
                    next_cursor=None,
                )

        fake = ChangedClient()
        with patch("mcp.Client", return_value=fake), self.assertRaises(RuntimeError):
            self.gateway.call("mcp_demo_lookup", {"company_name": "B"}, 20)
        fake.call_tool.assert_not_called()

    def test_cli_tools_and_both_demo_engines(self):
        config = self.root / "mcp.json"
        config.write_text(demo_config().model_dump_json(), encoding="utf-8")
        listed = subprocess.run(
            [sys.executable, "-B", "-m", "sourcing_agent", "mcp", "tools", "--config", str(config)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([t["name"] for t in json.loads(listed.stdout)], ["mcp_demo_lookup"])
        for engine in ENGINES:
            with self.subTest(engine=engine):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "sourcing_agent",
                        "mcp",
                        "demo",
                        "--engine",
                        engine,
                        "--state-dir",
                        str(self.root / f"cli-{engine}"),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("外部调查记录", completed.stdout)
                self.assertIn("status=completed model_calls=3 tool_calls=3", completed.stdout)

    def test_stdio_provenance_and_condition_invalidation(self):
        task = self.task()
        before = [c.model_dump() for c in task.state.candidates]
        summary = task.execute(
            "mcp_demo_lookup", {"supplier_id": 20, "arguments": {"company_name": "Demo Supplier B"}}
        )
        observation = task.state.external_observations[0]
        self.assertEqual(before, [c.model_dump() for c in task.state.candidates])
        self.assertEqual(len(observation.content_sha256), 64)
        self.assertEqual(observation.source_urls, ["https://example.invalid/synthetic-company"])
        self.assertNotIn("result", summary)
        self.assertLess(len(encode(summary)), 4000)
        self.assertEqual(
            task.execute("read_external_result", {"observation_id": observation.id})["result"],
            observation.result,
        )
        self.assertIn("外部调查记录", task.render())
        with (
            patch.object(self.gateway, "identity", "changed"),
            self.assertRaisesRegex(ValueError, "MCP 配置"),
        ):
            ResearchTask(self.repo, task.checkpoint(), "继续", self.gateway)
        other = ResearchTask(self.repo, {}, QUESTION, self.gateway)
        with self.assertRaises(ToolInputError):
            other.execute("read_external_result", {"observation_id": observation.id})
        task.execute("set_requirements", {"supplier_country": "United States"})
        self.assertEqual(task.state.external_observations, [])
        self.assertEqual(task.state.external_config_sha256, self.gateway.identity)
        with self.assertRaises(ToolInputError):
            task.execute("read_external_result", {"observation_id": observation.id})
        with patch.object(self.gateway, "call") as call, self.assertRaises(ToolInputError):
            task.execute("mcp_demo_lookup", {"supplier_id": 20, "arguments": {"company_name": "B"}})
        call.assert_not_called()

    def test_remote_errors_and_oversized_results_are_not_evidence(self):
        task = self.task()
        with self.assertRaises(ToolInputError):
            task.execute(
                "mcp_demo_lookup",
                {"supplier_id": 20, "arguments": {"company_name": "B", "detail_chars": -1}},
            )
        config = demo_config()
        config.servers[0].max_response_chars = 1024
        gateway = McpGateway(config)
        with self.assertRaises(ToolInputError):
            gateway.call("mcp_demo_lookup", {"company_name": "B", "detail_chars": 10000}, 20)
        self.assertEqual(task.state.external_observations, [])

    def test_timeout_and_transport_errors_are_bounded_and_sanitized(self):
        async def fail(*args):
            raise RuntimeError("Bearer TEST-SECRET-DO-NOT-LOG")

        with (
            patch.object(self.gateway, "_exchange", side_effect=fail),
            self.assertRaises(RuntimeError) as caught,
        ):
            self.gateway.call("mcp_demo_lookup", {"company_name": "B"}, 20)
        self.assertNotIn("TEST-SECRET", str(caught.exception))
        gateway = McpGateway(demo_config())
        gateway.config.servers[0].timeout_seconds = 0.1
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            gateway.call("mcp_demo_lookup", {"company_name": "B", "delay_seconds": 5}, 20)
        self.assertLess(time.monotonic() - started, 10)

    def test_both_engines_complete_in_three_calls(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                runner = create_runner(
                    self.snapshot,
                    self.root / engine,
                    "project",
                    McpDemoModel(),
                    limits=RunLimits(model_calls=3),
                    engine=engine,
                    external=self.gateway,
                )
                session = runner.create_session()
                result = runner.run(session.id, QUESTION)
                self.assertEqual(result.status, "completed")
                self.assertEqual((result.model_calls, result.tool_calls), (3, 3))
                self.assertEqual(len(result.task_state["external_observations"]), 1)
                self.assertEqual(
                    runner.store.load_session(session.id, "project").state, result.task_state
                )
                validate_tool_pairs(result.messages)

    def test_failure_rollback_and_engine_resume_semantics(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                response = actions(
                    ("mcp_demo_lookup", {"supplier_id": 20, "arguments": {"company_name": "B"}})
                )
                model = ScriptedModel(
                    actions(("find_supplier_candidates", {})),
                    response,
                    RuntimeError("model unavailable"),
                )
                runner = create_runner(
                    self.snapshot,
                    self.root / engine,
                    "project",
                    model,
                    engine=engine,
                    external=self.gateway,
                )
                session = runner.create_session()
                with self.assertRaises(RuntimeError):
                    runner.run(session.id, QUESTION)
                self.assertEqual(runner.store.load_session(session.id, "project").state, {})
                responses = (
                    []
                    if engine == "langgraph"
                    else [actions(("find_supplier_candidates", {})), response]
                )
                restarted = create_runner(
                    self.snapshot,
                    self.root / engine,
                    "project",
                    ScriptedModel(*responses, actions(("submit_findings", {}))),
                    engine=engine,
                    external=self.gateway,
                )
                with patch.object(self.gateway, "call", wraps=self.gateway.call) as call:
                    result = restarted.run(session.id, resume=True)
                self.assertEqual(result.status, "completed")
                self.assertEqual(call.call_count, 0 if engine == "langgraph" else 1)


@unittest.skipUnless(HAS_MCP, "optional MCP dependencies not installed")
class McpHttpTests(unittest.TestCase):
    def test_real_http_and_environment_header_auth(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}
        }
        env.update(
            PYTHONUTF8="1",
            PYTHONDONTWRITEBYTECODE="1",
            SOURCING_MCP_DEMO_AUTH="Bearer LOCAL-TEST-ONLY",
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "src/sourcing_agent/mcp_demo_server.py"),
                "--transport",
                "streamable-http",
                "--port",
                str(port),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail("Local MCP HTTP server exited")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("Local MCP HTTP server startup timeout")
            base = {
                "name": "local",
                "transport": "streamable-http",
                "url": f"http://127.0.0.1:{port}/mcp",
                "headers_env": {"Authorization": "SOURCING_TEST_AUTH"},
                "tools": [
                    {
                        "name": "lookup_company",
                        "alias": "lookup",
                        "description": "Local demo lookup",
                        "read_only": True,
                    }
                ],
            }
            with patch.dict(os.environ, {"SOURCING_TEST_AUTH": "Bearer LOCAL-TEST-ONLY"}):
                gateway = McpGateway(McpConfig.model_validate({"servers": [base]}))
                result = gateway.call("mcp_local_lookup", {"company_name": "Local demo"}, 20)
            self.assertIn("example.invalid", result.source_urls[0])
            self.assertNotIn("LOCAL-TEST-ONLY", encode(result.model_dump(mode="json")))
            with (
                patch.dict(os.environ, {"SOURCING_TEST_AUTH": "Bearer WRONG"}),
                self.assertRaises(RuntimeError),
            ):
                McpGateway(McpConfig.model_validate({"servers": [base]}))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
