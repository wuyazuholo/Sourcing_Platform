"""通过子进程验收公开 CLI，验证离线入口与持久化重启。"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "runtime"

    def command(self, *arguments: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONUTF8="1")
        return subprocess.run(
            [sys.executable, "-m", "sourcing_agent", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )

    def test_demo_and_restarted_session_can_be_inspected(self) -> None:
        result = self.command("demo", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=completed", result.stdout)
        self.assertIn("SYNTHETIC DEMO", result.stdout)
        session_id = result.stdout.splitlines()[0].split(": ")[1]
        inspected = self.command(
            "session", "show", "--session", session_id, "--state-dir", str(self.state)
        )
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)["status"], "completed")
        events = self.command(
            "session", "events", "--session", session_id, "--state-dir", str(self.state)
        )
        self.assertEqual(events.returncode, 0, events.stderr)
        kinds = {event["type"] for event in json.loads(events.stdout)}
        self.assertTrue({"model.request", "tool.result", "run.finished"}.issubset(kinds))

    def test_memory_lifecycle_across_cli_processes(self) -> None:
        base = ("--state-dir", str(self.state), "--namespace", "project-a")
        saved = self.command("memory", "set", *base, "--key", "采购偏好", "--value", "优先中国")
        self.assertEqual(saved.returncode, 0, saved.stderr)
        listed = self.command("memory", "list", *base)
        self.assertEqual(json.loads(listed.stdout)[0]["value"], "优先中国")
        deleted = self.command("memory", "delete", *base, "--key", "采购偏好")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(json.loads(self.command("memory", "list", *base).stdout), [])

    def test_demo_resume_requires_explicit_session_and_no_new_message(self) -> None:
        result = self.command("demo", "--resume", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--session", result.stderr)

    def test_offline_doctor_reads_snapshot_from_explicit_env_file(self) -> None:
        demo = self.command("demo", "--state-dir", str(self.state))
        self.assertEqual(demo.returncode, 0, demo.stderr)
        env_file = self.state / ".env.local"
        env_file.write_text(
            'SOURCING_TRADE_SNAPSHOT="demo-trades.sqlite"\nSOURCING_MODEL_PROVIDER="openai"\n',
            encoding="utf-8",
        )
        result = self.command("doctor", "--env-file", str(env_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["snapshot"]["row_count"], 6)
        self.assertFalse(report["live_requested"])

    @unittest.skipUnless(importlib.util.find_spec("langgraph"), "optional graph dependencies")
    def test_both_cli_engines_deliver_same_demo_report(self) -> None:
        native = self.command("demo", "--engine", "native", "--state-dir", str(self.state))
        graph = self.command("demo", "--engine", "langgraph", "--state-dir", str(self.state))
        self.assertEqual(native.returncode, 0, native.stderr)
        self.assertEqual(graph.returncode, 0, graph.stderr)
        self.assertEqual(native.stdout.splitlines()[3:], graph.stdout.splitlines()[3:])
        native_id = native.stdout.splitlines()[0].split(": ")[1]
        wrong = self.command(
            "demo", "--engine", "langgraph", "--session", native_id, "--state-dir", str(self.state)
        )
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("相同 --engine", wrong.stderr)


if __name__ == "__main__":
    unittest.main()
