"""SQL Server configuration and read contracts without any real credentials or network."""

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sourcing_agent.adapters.snapshot import demo_records, publish_snapshot
from sourcing_agent.adapters.sqlserver import PROJECTION, SqlServerReader, extraction_query
from sourcing_agent.config import load_environment
from sourcing_agent.database_config import database_config
from sourcing_agent.database_mcp import create_database_server
from sourcing_agent.sourcing.database_search import search_database
from sourcing_agent.sourcing.models import Requirements

SETTINGS = {
    "SOURCING_DB_HOST": "db.example.invalid",
    "SOURCING_DB_NAME": "XpressfeedDB",
    "SOURCING_DB_USER": "LOCAL-TEST-USER",
    "SOURCING_DB_PASSWORD": "LOCAL-TEST-PASSWORD",
}


def requirements(**updates):
    return Requirements.model_validate(
        {"hs_prefix": "841891", "start_date": "2025-01-01", "end_date": "2025-12-31", **updates}
    )


class DatabaseConfigurationTests(unittest.TestCase):
    def test_secrets_are_excluded_from_repr_and_serialized_metadata(self):
        with patch.dict(os.environ, SETTINGS, clear=True):
            config = database_config()
        for secret in [SETTINGS["SOURCING_DB_USER"], SETTINGS["SOURCING_DB_PASSWORD"]]:
            self.assertNotIn(secret, repr(config))
            self.assertNotIn(secret, config.model_dump_json())
        self.assertEqual(config.database, "XpressfeedDB")

    def test_missing_or_invalid_configuration_is_explicit_and_redacted(self):
        for updates in [{"SOURCING_DB_PASSWORD": ""}, {"SOURCING_DB_PORT": "NOT-A-PORT"}]:
            with (
                patch.dict(os.environ, {**SETTINGS, **updates}, clear=True),
                self.assertRaises(ValueError) as caught,
            ):
                database_config()
            self.assertNotIn("LOCAL-TEST-PASSWORD", str(caught.exception))

    def test_local_env_preserves_special_characters(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env.database.local"
            path.write_text("SOURCING_DB_PASSWORD='literal$PASSWORD;#{}'\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {k: v for k, v in SETTINGS.items() if k != "SOURCING_DB_PASSWORD"},
                clear=True,
            ):
                load_environment(path)
                self.assertEqual(
                    database_config().password.get_secret_value(), "literal$PASSWORD;#{}"
                )


class DatabaseConnectionTests(unittest.TestCase):
    def reader(self):
        with patch.dict(os.environ, SETTINGS, clear=True):
            return SqlServerReader(database_config())

    def test_metadata_is_parameterized_timeout_bounded_and_connections_closed(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("XpressfeedDB",)
        cursor.fetchall.side_effect = [[("dbo", "panjivaMXExport")], [("id", "bigint", False, 8)]]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        fake = SimpleNamespace(
            connect=MagicMock(return_value=connection), Error=type("DriverError", (Exception,), {})
        )
        with patch.dict("sys.modules", {"pymssql": fake}):
            report = self.reader().describe()
        self.assertTrue(report["ok"])
        self.assertEqual(fake.connect.call_args.kwargs["timeout"], 15)
        self.assertTrue(fake.connect.call_args.kwargs["read_only"])
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()
        calls = cursor.execute.call_args_list
        self.assertTrue(all(c.args[0].startswith(("SET LOCK_TIMEOUT", "SELECT ")) for c in calls))
        self.assertEqual(calls[-1].args[1], ("dbo", "panjivaMXExport"))
        self.assertNotIn("LOCAL-TEST", json.dumps(report))

    def test_driver_and_cleanup_errors_never_expose_credentials(self):
        error_type = type("DriverError", (Exception,), {})
        connection = MagicMock()
        connection.cursor.side_effect = error_type("PWD=LOCAL-TEST-PASSWORD")
        connection.rollback.side_effect = error_type("UID=LOCAL-TEST-USER")
        fake = SimpleNamespace(connect=MagicMock(return_value=connection), Error=error_type)
        with (
            patch.dict("sys.modules", {"pymssql": fake}),
            self.assertRaises(RuntimeError) as caught,
        ):
            with self.reader().connection():
                pass
        self.assertNotIn("LOCAL-TEST", str(caught.exception))
        connection.close.assert_called_once()

    def test_package_import_does_not_require_database_driver(self):
        # Import-time database access is prohibited; all imports above work without a driver.
        self.assertIsNotNone(importlib.util.find_spec("sourcing_agent.bootstrap"))

    def test_temporary_driver_configuration_is_restored_after_failure(self):
        error_type = type("DriverError", (Exception,), {})
        fake = SimpleNamespace(
            connect=MagicMock(side_effect=error_type("LOCAL-TEST-PASSWORD")), Error=error_type
        )
        with (
            patch.dict(os.environ, {"FREETDSCONF": "original-path"}),
            patch.dict("sys.modules", {"pymssql": fake}),
        ):
            with self.assertRaises(RuntimeError), self.reader().connection():
                pass
            self.assertEqual(os.environ["FREETDSCONF"], "original-path")


class ExtractionTests(unittest.TestCase):
    def test_window_precedes_filters_and_all_user_inputs_are_parameters(self):
        query, params = extraction_query(
            requirements(
                product_text="50%_[x]\\'; DROP TABLE trades--",
                market_country="CO';--",
                exclude_supplier_id=42,
            ),
            7,
            200000,
        )
        self.assertTrue(query.startswith("WITH source_window AS (SELECT TOP (%s)"))
        self.assertEqual(query.count("%s"), len(params))
        self.assertEqual(params[:4], (200000, 8, date(2025, 1, 1), date(2026, 1, 1)))
        self.assertIn("%[^0-9]841891%", params)
        self.assertIn("%50\\%\\_\\[x\\]\\\\'; DROP TABLE trades--%", params)
        self.assertNotIn("DROP TABLE", query)
        self.assertNotIn("CO';--", query)
        self.assertEqual(params[-1], 42)

    def test_full_history_keeps_result_limit_and_inclusive_end_day(self):
        query, params = extraction_query(requirements(), 10, 0)
        self.assertEqual(query.count("TOP (%s)"), 1)
        self.assertEqual(params[:3], (11, date(2025, 1, 1), date(2026, 1, 1)))
        self.assertIn("[shipment_date]<%s", query)

    def test_invalid_bounds_and_missing_filters_fail_before_connect(self):
        cases = [
            (requirements(), 0, 1),
            (requirements(), True, 1),
            (requirements(), 1, -1),
            (requirements(), 1, 2000001),
            (requirements(end_date="9999-12-31"), 1, 1),
            (Requirements(), 1, 1),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                extraction_query(*args)

    def test_row_mapping_and_extra_row_detect_truncation(self):
        row = tuple(demo_records()[0].model_dump()[name] for name in PROJECTION)
        connection = MagicMock()
        connection.cursor.return_value.fetchmany.return_value = [row, row]
        with patch.dict(os.environ, SETTINGS, clear=True):
            reader = SqlServerReader(database_config())
        with patch.object(reader, "connection") as context:
            context.return_value.__enter__.return_value = connection
            records, truncated = reader.extract(requirements(), max_records=1)
        self.assertTrue(truncated)
        self.assertEqual(records, [demo_records()[0]])
        connection.cursor.return_value.fetchmany.assert_called_once_with(2)

    def test_invalid_source_row_is_rejected_without_echoing_contents(self):
        connection = MagicMock()
        connection.cursor.return_value.fetchmany.return_value = [("PRIVATE-ROW",)]
        with patch.dict(os.environ, SETTINGS, clear=True):
            reader = SqlServerReader(database_config())
        with patch.object(reader, "connection") as context:
            context.return_value.__enter__.return_value = connection
            with self.assertRaises(ValueError) as caught:
                reader.extract(requirements())
        self.assertNotIn("PRIVATE-ROW", str(caught.exception))


class DatabaseReportTests(unittest.TestCase):
    def test_both_engines_bind_evidence_to_snapshot_and_warn_about_truncation(self):
        engines = ["native"] + (["langgraph"] if importlib.util.find_spec("langgraph") else [])
        with patch.dict(os.environ, SETTINGS, clear=True):
            config = database_config()
        for engine in engines:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as folder:
                with patch.object(
                    SqlServerReader, "extract", return_value=(demo_records()[:3], True)
                ) as extract:
                    report = search_database(config, requirements(), Path(folder), engine=engine)
                extract.assert_called_once()
                self.assertTrue(report["ok"])
                self.assertEqual(len(report["candidates"]), 2)
                self.assertFalse(report["business_data_sent_to_model"])
                self.assertIn("保存上限", report["text"])
                self.assertIn("不覆盖整张表", report["text"])
                self.assertTrue(report["evidence"])
                profiles = {row["supplier_id"]: row for row in report["candidate_profiles"]}
                for candidate in report["candidates"]:
                    profile = profiles[candidate["supplier_id"]]
                    self.assertEqual(profile["record_count"], candidate["record_count"])
                    self.assertEqual(
                        sum(row["record_count"] for row in profile["monthly_activity"]),
                        candidate["record_count"],
                    )
                self.assertEqual(profiles[20]["missing_buyer_ids"], 1)
                self.assertEqual(profiles[20]["buyers"], [])
                for evidence in report["evidence"]:
                    self.assertEqual(
                        evidence["source"]["snapshot_sha256"], report["snapshot_sha256"]
                    )
                self.assertNotIn("LOCAL-TEST", json.dumps(report))

    def test_empty_result_is_explicit_and_snapshot_is_published(self):
        with patch.dict(os.environ, SETTINGS, clear=True):
            config = database_config()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(SqlServerReader, "extract", return_value=([], False)),
        ):
            report = search_database(config, requirements(), Path(folder))
            self.assertTrue(report["ok"])
            self.assertEqual(report["candidates"], [])
            self.assertEqual(report["candidate_profiles"], [])
            self.assertEqual(report["record_count"], 0)
            self.assertTrue(Path(report["snapshot"]).is_file())

    def test_source_failure_does_not_publish_partial_snapshot(self):
        with patch.dict(os.environ, SETTINGS, clear=True):
            config = database_config()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(SqlServerReader, "extract", side_effect=ValueError("invalid row")),
        ):
            with self.assertRaises(ValueError):
                search_database(config, requirements(), Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_snapshot_metadata_is_protected_and_empty_requires_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trades.sqlite"
            with self.assertRaises(ValueError):
                publish_snapshot(path, [], "scope")
            with self.assertRaises(ValueError):
                publish_snapshot(
                    path, [], "scope", allow_empty=True, extra_metadata={"row_count": "999"}
                )
            self.assertFalse(path.exists())
            publish_snapshot(
                path, [], "scope", allow_empty=True, extra_metadata={"source_kind": "sqlserver"}
            )
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    dict(db.execute("SELECT key,value FROM metadata"))["source_kind"], "sqlserver"
                )


@unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP SDK not installed")
class DatabaseMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_returns_structured_report_and_fixed_read_only_tool(self):
        with patch.dict(os.environ, SETTINGS, clear=True):
            config = database_config()
        with tempfile.TemporaryDirectory() as folder:
            server = create_database_server(config, Path(folder))
            tools = await server.list_tools()
            self.assertEqual([tool.name for tool in tools], ["search_trade_suppliers"])
            self.assertTrue(tools[0].annotations.read_only_hint)
            self.assertIsNotNone(tools[0].output_schema)
            with patch.object(
                SqlServerReader, "extract", return_value=(demo_records()[:3], False)
            ) as extract:
                result = await server.call_tool(
                    "search_trade_suppliers",
                    {"hs_prefix": "841891", "start_date": "2025-01-01", "end_date": "2025-12-31"},
                )
            self.assertTrue(result.structured_content["ok"])
            self.assertEqual(result.structured_content["record_count"], 3)
            self.assertNotIn("text", result.structured_content)
            self.assertFalse(result.structured_content["business_data_sent_to_model"])
            self.assertEqual(extract.call_args.args[1:], (5000, 200000))


if __name__ == "__main__":
    unittest.main()
