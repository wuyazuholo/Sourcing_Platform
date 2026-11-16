"""本地配置、DeepSeek 扩展参数和数据迁移的回归验收。"""

import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sourcing_agent.adapters.snapshot import demo_records, publish_snapshot
from sourcing_agent.config import (
    load_environment,
    request_options,
    selected_provider,
    selected_snapshot,
)


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_explicit_env_file_preserves_process_environment(self) -> None:
        env_file = self.root / ".env.local"
        env_file.write_text(
            'SOURCING_MODEL_NAME="file-model"\nSOURCING_MODEL_PROVIDER="openai"\n', encoding="utf-8"
        )
        with patch.dict(os.environ, {"SOURCING_MODEL_NAME": "process-model"}, clear=True):
            load_environment(env_file)
            self.assertEqual(os.environ["SOURCING_MODEL_NAME"], "process-model")
            self.assertEqual(selected_provider(None), "openai")
            self.assertEqual(selected_provider("anthropic"), "anthropic")

    def test_snapshot_path_is_relative_to_env_file_not_current_directory(self) -> None:
        snapshot = self.root / "data/trades.sqlite"
        snapshot.parent.mkdir()
        publish_snapshot(snapshot, demo_records(), "synthetic")
        with patch.dict(os.environ, {"SOURCING_TRADE_SNAPSHOT": "data/trades.sqlite"}, clear=True):
            self.assertEqual(selected_snapshot(None, self.root / ".env.local"), snapshot.resolve())

    def test_invalid_provider_and_missing_snapshot_are_explicit_errors(self) -> None:
        with patch.dict(os.environ, {"SOURCING_MODEL_PROVIDER": "unknown"}, clear=True):
            with self.assertRaises(ValueError):
                selected_provider(None)
            with self.assertRaises(ValueError):
                selected_snapshot(None, None)

    def test_deepseek_thinking_option_is_parsed_and_control_fields_cannot_be_overridden(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {"SOURCING_MODEL_REQUEST_OPTIONS": '{"thinking":{"type":"disabled"}}'},
            clear=True,
        ):
            self.assertEqual(request_options(), {"thinking": {"type": "disabled"}})
        for value in ('{"messages":[]}', '{"max_tokens":99999}', "[]", "invalid-json"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"SOURCING_MODEL_REQUEST_OPTIONS": value}, clear=True),
                self.assertRaises(ValueError),
            ):
                request_options()

    def test_empty_env_values_do_not_expand_another_environment_secret(self) -> None:
        env_file = self.root / ".env.local"
        env_file.write_text('SOURCING_MODEL_NAME="$OTHER_SECRET"\n', encoding="utf-8")
        with patch.dict(os.environ, {"OTHER_SECRET": "do-not-interpolate"}, clear=True):
            load_environment(env_file)
            self.assertEqual(os.environ["SOURCING_MODEL_NAME"], "$OTHER_SECRET")


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        (self.target / "src/sourcing_agent").mkdir(parents=True)
        data = self.source / "var/supply-chain"
        data.mkdir(parents=True)
        publish_snapshot(data / "trades.sqlite", demo_records(), "synthetic")
        (data / "coimport.json").write_text(
            json.dumps([r.model_dump(mode="json") for r in demo_records()]), encoding="utf-8"
        )
        (self.source / "backend/configs").mkdir(parents=True)
        (self.source / "backend/configs/supply-chain.toml").write_text(
            '[model]\nname="deepseek-flash"\nbase_url="https://api.deepseek.com"\n'
            'api_key_env="FLOWPAY_MODEL_API_KEY"\n'
            '[model.request_options]\nmax_tokens=4096\nthinking={type="disabled"}\n',
            encoding="utf-8",
        )
        (self.source / ".env").write_text(
            "FLOWPAY_MODEL_API_KEY=test-only-not-a-real-key\n", encoding="utf-8"
        )
        script = Path(__file__).resolve().parents[1] / "scripts/migrate_flowpay.py"
        self.migrate = runpy.run_path(str(script))["migrate"]

    def test_migration_checks_hashes_and_never_logs_key_in_manifest(self) -> None:
        result = self.migrate(self.source, self.target)
        self.assertEqual(result["row_count"], 6)
        self.assertTrue(all(a["verified"] for a in result["assets"]))
        self.assertNotIn("test-only-not-a-real-key", json.dumps(result))
        self.assertEqual(
            (self.source / "var/supply-chain/trades.sqlite").read_bytes(),
            (self.target / ".runtime/data/trades.sqlite").read_bytes(),
        )
        with patch.dict(os.environ, {}, clear=True):
            load_environment(self.target / ".env.local")
            self.assertEqual(os.environ["SOURCING_MODEL_API_KEY"], "test-only-not-a-real-key")
            self.assertEqual(request_options(), {"thinking": {"type": "disabled"}})

    def test_repeat_migration_is_idempotent(self) -> None:
        first = self.migrate(self.source, self.target)
        credential_bytes = (self.target / ".env.local").read_bytes()
        second = self.migrate(self.source, self.target)
        self.assertEqual(first["assets"], second["assets"])
        self.assertEqual((self.target / ".env.local").read_bytes(), credential_bytes)

    def test_existing_different_credentials_are_not_overwritten(self) -> None:
        env_file = self.target / ".env.local"
        env_file.write_text("SOURCING_MODEL_API_KEY=keep-existing\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.migrate(self.source, self.target)
        self.assertEqual(
            env_file.read_text(encoding="utf-8"), "SOURCING_MODEL_API_KEY=keep-existing\n"
        )
        self.assertFalse((self.target / ".runtime/data/trades.sqlite").exists())

    def test_existing_different_data_is_not_overwritten(self) -> None:
        destination = self.target / ".runtime/data/trades.sqlite"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing-data")
        with self.assertRaises(ValueError):
            self.migrate(self.source, self.target)
        self.assertEqual(destination.read_bytes(), b"existing-data")
        self.assertFalse((self.target / ".env.local").exists())


if __name__ == "__main__":
    unittest.main()
