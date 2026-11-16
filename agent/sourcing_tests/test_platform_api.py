"""Real loopback HTTP tests; database and remote models are never called."""

import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from pydantic import SecretStr

from sourcing_agent.adapters.snapshot import demo_records, publish_snapshot
from sourcing_agent.adapters.sqlserver import extraction_query
from sourcing_agent.adapters.trade import TradeRepository, hs_matches
from sourcing_agent.database_config import DatabaseConfig
from sourcing_agent.platform_api import PlatformServer, public_report
from sourcing_agent.sourcing.models import Requirements

TOKEN = "local-test-token-is-at-least-thirty-two-characters"
CONFIG = DatabaseConfig(
    host="db.invalid", user=SecretStr("test-user"), password=SecretStr("test-password")
)
REQUEST = {
    "owner": "buyer-one",
    "requirements": {"hs_prefix": "8544", "start_date": "2025-01-01", "end_date": "2025-12-31"},
}


class PlatformApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.mock = patch(
            "sourcing_agent.platform_api.search_database",
            return_value={
                "ok": True,
                "table": "dbo.panjivaMXExport",
                "snapshot": "PRIVATE-PATH",
                "state_dir": "PRIVATE-DIR",
                "candidates": [{"supplier_id": 9007199254740993}],
            },
        )
        self.search = self.mock.start()
        self.server = PlatformServer(0, TOKEN, CONFIG, Path(self.folder.name))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.mock.stop()
        self.folder.cleanup()

    def request(self, path, body=None, token=TOKEN):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            return response.code, json.load(response)

    def completed(self, identifier):
        for _ in range(100):
            code, job = self.request(f"/v1/searches/{identifier}?owner=buyer-one")
            self.assertEqual(code, 200)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail("Job did not complete")

    def test_all_routes_require_service_token(self):
        self.assertEqual(self.request("/health", token="incorrect")[0], 401)
        self.assertEqual(self.request("/v1/searches", REQUEST, token="")[0], 401)
        self.search.assert_not_called()

    def test_owner_scoping_bigint_and_path_redaction_over_real_http(self):
        code, job = self.request("/v1/searches", REQUEST)
        self.assertEqual(code, 202)
        result = self.completed(job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["candidates"][0]["supplier_id"], "9007199254740993")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("owner", result)
        self.assertEqual(self.request(f"/v1/searches/{job['id']}?owner=buyer-two")[0], 404)
        self.assertEqual(self.request(f"/v1/searches/{job['id']}")[0], 404)

    def test_invalid_requests_never_reach_database_or_create_work(self):
        cases = [
            {**REQUEST, "sql": "SELECT * FROM arbitrary"},
            {**REQUEST, "max_records": True},
            {**REQUEST, "scan_limit": 2000001},
            {**REQUEST, "requirements": {}},
            {**REQUEST, "requirements": {**REQUEST["requirements"], "start_date": "2026-01-01"}},
            {**REQUEST, "owner": "../../other"},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.request("/v1/searches", body)[0], 400)
        self.search.assert_not_called()

    def test_failure_is_not_presented_as_empty_success_or_raw_driver_error(self):
        self.search.side_effect = RuntimeError("PASSWORD=PRIVATE-SECRET")
        _, job = self.request("/v1/searches", REQUEST)
        result = self.completed(job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertIn("不表示没有匹配", result["error"])

    def test_health_identifies_mexico_and_does_not_query_database(self):
        code, result = self.request("/health")
        self.assertEqual(code, 200)
        self.assertEqual(result["table"], "dbo.panjivaMXExport")
        self.search.assert_not_called()


class MexicoMappingTests(unittest.TestCase):
    def test_spanish_case_and_literal_wildcards_survive_snapshot_filtering(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "spanish.sqlite"
            record = demo_records()[0].model_copy(
                update={
                    "source_table": "dbo.panjivaMXExport",
                    "goods_description": "PLÁSTICO 100%_REAL",
                }
            )
            publish_snapshot(path, [record], "Spanish fixture")
            repository = TradeRepository(path)
            req = Requirements(
                product_text="plástico 100%_real", start_date="2025-01-01", end_date="2025-12-31"
            )
            self.assertEqual(len(repository.find_candidates(req)), 1)
            self.assertEqual(
                repository.find_candidates(req.model_copy(update={"product_text": "100%_other"})),
                [],
            )

    def test_live_sql_uses_reviewed_mexico_table_and_value_field(self):
        query, _ = extraction_query(
            Requirements(hs_prefix="8544", start_date="2025-01-01", end_date="2025-12-31"), 10, 1000
        )
        self.assertIn("[dbo].[panjivaMXExport]", query)
        self.assertIn("[valueOfGoodsItemCIFUSD]", query)
        self.assertNotIn("COImport", query)

    def test_multiple_and_dotted_hs_codes_have_same_snapshot_semantics(self):
        self.assertEqual(hs_matches("123456; 85.44.11", "8544"), 1)
        self.assertEqual(hs_matches("1854411000", "8544"), 0)
        self.assertEqual(hs_matches(None, "8544"), 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mexico.sqlite"
            row = demo_records()[0].model_copy(
                update={
                    "source_table": "dbo.panjivaMXExport",
                    "hs_code": "123456; 85.44.11",
                    "supplier_country": "Mexico",
                }
            )
            publish_snapshot(path, [row], "Mexico unit fixture")
            repository = TradeRepository(path)
            req = Requirements(hs_prefix="8544", start_date="2025-01-01", end_date="2025-12-31")
            self.assertEqual(len(repository.find_candidates(req)), 1)
            self.assertEqual(
                repository.evidence(req, row.supplier_id)[0].source.source_table,
                "dbo.panjivaMXExport",
            )

    def test_nested_evidence_ids_preserve_browser_precision(self):
        report = public_report(
            {
                "evidence": [{"source": {"record_id": 9007199254740993, "buyer_id": None}}],
                "record_count": 10,
            }
        )
        self.assertEqual(report["evidence"][0]["source"]["record_id"], "9007199254740993")
        self.assertIsNone(report["evidence"][0]["source"]["buyer_id"])
        self.assertEqual(report["record_count"], 10)
