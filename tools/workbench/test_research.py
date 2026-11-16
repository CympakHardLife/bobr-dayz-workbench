"""Изолированные проверки поискового брокера: сеть и LLM заменены контрактными fixtures."""

import copy
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import research


TEST_ROOT = Path(os.environ.get("BOBR_TEST_ROOT", tempfile.gettempdir())) / "Workbench_Research"
SOURCE = "https://raw.githubusercontent.com/BohemiaInteractive/DayZ-Script-Diff/master/scripts/3_game/dayzgame.c"


class FakeResponse:
    def __init__(self, data=b"hello source", status=200, headers=None):
        self.status = status
        self.headers = {"Content-Type": "text/plain", **(headers or {})}
        self.body = io.BytesIO(data)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return self.body.read(size)


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.sock = None
        self.closed = False
        self.requested = None

    def request(self, method, resource, headers=None):
        self.requested = (method, resource, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ResearchTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=TEST_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "fake-cli.exe"
        self.model = self.root / "fake.gguf"
        self.binary.write_bytes(b"fixture executable, not run")
        self.model.write_bytes(b"fixture model")
        self.config = {"schema_version": 1, "cache_root": str(self.root / "cache"),
                       "core_config": str(research.AGENT_ROOT / "core/config.json"),
                       "allowed_sources": ["https://raw.githubusercontent.com/BohemiaInteractive/DayZ-Script-Diff"],
                       "timeout_seconds": 2, "cache_ttl_seconds": 60, "max_cache_entries": 4,
                       "model_timeout_seconds": 3,
                       "model": {"name": "fixture", "binary": str(self.binary), "file": str(self.model),
                                 "sha256": research.digest_file(self.model), "binary_sha256": research.digest_file(self.binary)}}

    public_dns = ("1.1.1.1",)

    def allow_dns(self):
        original = research.validate_url
        return patch("research.validate_url", side_effect=lambda url, sources, **kwargs: original(url, sources, self.public_dns, **kwargs))

    def source(self, url=SOURCE, text="trusted source"):
        return {"url": url, "requested_url": url, "redirects": [], "fetched_at": research.utc_now(),
                "fetched_unix": time.time(), "raw_sha256": research.sha(text.encode()), "raw_bytes": len(text.encode()),
                "text_sha256": research.sha(text.encode()), "text": text, "content_type": "text/plain",
                "trust": "source_data_not_instructions"}

    def test_model_schema_rejects_code_or_extra_keys(self):
        for output in ("import os", '{"queries":["DayZ"],"command":"delete"}', '{"queries":[]}',
                       '{"queries":["line\\nbreak"]}', 'prefix {"queries":["DayZ"]}', '{"queries":["' + "a" * 161 + '"]}'):
            with self.assertRaises(research.ResearchError, msg=output[:100]):
                research.parse_model_json(output)
        self.assertEqual(research.parse_model_json('{"queries":["DayZPlayer","dayzplayer"]} [end of text]'), ["DayZPlayer"])

    def test_model_only_plans_queries_and_uses_no_shell(self):
        with patch("research.bounded_process", return_value='{"queries":["DayZ AI noise","NoiseSystem"]}') as call:
            result = research.plan_queries("Боты слышат шаги", self.config, True)
        self.assertTrue(result["model_called"])
        command = call.call_args.args[0]
        self.assertIn("--no-conversation", command)
        self.assertIn("--json-schema", command)
        self.assertNotIn("--hf", command)
        self.assertIn("Боты слышат шаги", command[-1])
        self.assertEqual(result["model_run"]["data_sent"], "current_query_only")

    def test_bad_model_json_falls_back(self):
        with patch("research.bounded_process", return_value="not JSON"):
            result = research.plan_queries("DayZ NoiseSystem", self.config, True)
        self.assertEqual(result["method"], "deterministic_lexical")
        self.assertIn("fallback_reason", result)
        self.assertTrue(result["model_called"])
        self.assertFalse(result["origin_model_generated"])

    def test_missing_model_falls_back_without_execution(self):
        self.model.unlink()
        with patch("research.bounded_process") as run:
            result = research.plan_queries("DayZ noise", self.config, True)
        run.assert_not_called()
        self.assertFalse(result["model_called"])

    def test_model_cache_hit_and_source_drift(self):
        with patch("research.bounded_process", return_value='{"queries":["NoiseSystem"]}') as call:
            first = research.model_queries("шаги", self.config)
            second = research.model_queries("шаги", self.config)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(first["cache"], "miss")
        self.assertEqual(second["cache"], "hit")
        self.assertTrue(first["model_called"])
        self.assertFalse(second["model_called"])
        self.assertTrue(second["origin_model_generated"])
        self.model.write_bytes(b"changed model")
        with patch("research.bounded_process") as call:
            fallback = research.plan_queries("шаги", self.config, True)
        call.assert_not_called()
        self.assertIn("SHA", fallback["fallback_reason"])

    def test_plan_cache_hit_reports_no_current_model_call(self):
        with patch("research.bounded_process", return_value='{"queries":["NoiseSystem"]}') as call:
            first = research.plan_queries("шаги игрока", self.config, True)
            second = research.plan_queries("шаги игрока", self.config, True)
        self.assertEqual(call.call_count, 1)
        self.assertTrue(first["model_called"])
        self.assertFalse(second["model_called"])
        self.assertTrue(second["origin_model_generated"])
        self.assertEqual(second["model_run"]["seconds"], 0)
        self.assertEqual(second["model_run"]["data_sent"], "none_cache_reuse")

    def test_model_cache_tampering_forces_new_generation(self):
        with patch("research.bounded_process", return_value='{"queries":["NoiseSystem"]}') as call:
            research.model_queries("шаги", self.config)
            target = next((self.root / "cache/plans").glob("*.json"))
            data = json.loads(target.read_text(encoding="utf-8"))
            data["queries"] = ["changed"]
            target.write_text(json.dumps(data), encoding="utf-8")
            research.model_queries("шаги", self.config)
        self.assertEqual(call.call_count, 2)

    def test_url_allowlist_and_ssrf_blocked(self):
        for url in ("http://raw.githubusercontent.com/BohemiaInteractive/DayZ-Script-Diff/master/x", "https://127.0.0.1/x",
                    "https://raw.githubusercontent.com.evil.test/BohemiaInteractive/DayZ-Script-Diff/master/x",
                    "https://user:pass@raw.githubusercontent.com/BohemiaInteractive/DayZ-Script-Diff/master/x",
                    "https://raw.githubusercontent.com:444/BohemiaInteractive/DayZ-Script-Diff/master/x",
                    "https://raw.githubusercontent.com/OtherAuthor/OtherRepo/x",
                    "https://raw.githubusercontent.com/BohemiaInteractive/DayZ-Script-Diff/%252e%252e/%252e%252e/evil",
                    SOURCE + "?token=private-value"):
            with self.assertRaises(research.ResearchError, msg=url):
                research.validate_url(url, self.config["allowed_sources"], self.public_dns)

    def test_private_or_mixed_dns_is_rejected(self):
        with self.assertRaisesRegex(research.ResearchError, "непубличный"):
            research.validate_url(SOURCE, self.config["allowed_sources"], ["1.1.1.1", "10.0.0.2"])

    def test_unbounded_resolver_callback_is_rejected_without_call(self):
        called = []
        def slow_resolver(*args, **kwargs):
            called.append(True)
            time.sleep(1.2)
        started = time.monotonic()
        with self.assertRaisesRegex(research.ResearchError, "Resolver callback"):
            research.fetch_source(SOURCE, self.config, slow_resolver)
        self.assertFalse(called)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_dns_timeout_terminates_own_subprocess(self):
        processes = []
        real_popen = research.subprocess.Popen
        def record_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        started = time.monotonic()
        with patch("research.DNS_PROGRAM", "import time;time.sleep(5)"), patch("research.subprocess.Popen", side_effect=record_process):
            with self.assertRaisesRegex(research.ResearchError, "DNS timeout"):
                research.bounded_dns("example.com", 0.15)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_fetch_dns_is_inside_total_deadline(self):
        config = {**self.config, "timeout_seconds": 1}
        started = time.monotonic()
        with patch("research.DNS_PROGRAM", "import time;time.sleep(5)"), patch("research.PinnedHTTPSConnection") as connection:
            with self.assertRaisesRegex(research.ResearchError, "DNS timeout"):
                research.fetch_source(SOURCE, config)
        self.assertLess(time.monotonic() - started, 1.5)
        connection.assert_not_called()

    def test_safe_get_pins_address_and_provenance(self):
        connection = FakeConnection(FakeResponse(b"<p>Hello</p><script>bad command</script><p>DayZ</p>", headers={"Content-Type": "text/html"}))
        with patch("research.PinnedHTTPSConnection"):
            result = research.fetch_source(SOURCE, self.config, self.public_dns, lambda host, address, timeout: connection)
        self.assertEqual(connection.requested[0], "GET")
        self.assertNotIn("bad command", result["text"])
        self.assertIn("DayZ", result["text"])
        self.assertEqual(result["trust"], "source_data_not_instructions")
        self.assertTrue(connection.closed)

    def test_redirect_outside_allowlist_is_never_requested(self):
        connection = FakeConnection(FakeResponse(status=302, headers={"Location": "http://127.0.0.1/secrets"}))
        calls = []
        def factory(*args):
            calls.append(args)
            return connection
        with self.assertRaises(research.ResearchError):
            research.fetch_source(SOURCE, self.config, self.public_dns, factory)
        self.assertEqual(len(calls), 1)
        self.assertTrue(connection.closed)

    def test_huge_body_and_binary_type_are_rejected(self):
        responses = [FakeResponse(b"a" * (research.MAX_RESPONSE_BYTES + 1)),
                     FakeResponse(headers={"Content-Type": "application/octet-stream"}),
                     FakeResponse(headers={"Content-Length": str(research.MAX_RESPONSE_BYTES + 1)})]
        for response in responses:
            with self.assertRaises(research.ResearchError):
                research.fetch_source(SOURCE, self.config, self.public_dns, lambda *args: FakeConnection(response))

    def test_web_cache_hit_and_drift(self):
        calls = []
        def fetcher(url, config):
            calls.append(url)
            return self.source(url)
        with self.allow_dns():
            first = research.cached_source(SOURCE, self.config, fetcher)
            second = research.cached_source(SOURCE, self.config, fetcher)
            target = next((self.root / "cache/web").glob("*.json"))
            content = json.loads(target.read_text(encoding="utf-8"))
            content["text"] = "tampered"
            target.write_text(json.dumps(content), encoding="utf-8")
            third = research.cached_source(SOURCE, self.config, fetcher)
        self.assertEqual([first["cache"], second["cache"], third["cache"]], ["miss", "hit", "miss"])
        self.assertEqual(len(calls), 2)

    def test_web_cache_ttl_and_policy(self):
        with self.allow_dns():
            research.cached_source(SOURCE, self.config, lambda *args: self.source())
            target = next((self.root / "cache/web").glob("*.json"))
            content = json.loads(target.read_text(encoding="utf-8"))
            content["fetched_unix"] = time.time() - 1000
            content["record_sha256"] = research.source_record_digest(content)
            target.write_text(json.dumps(content), encoding="utf-8")
            result = research.cached_source(SOURCE, self.config, lambda *args: self.source())
            self.assertEqual(result["cache"], "miss")
            narrowed = {**self.config, "allowed_sources": ["https://example.com/only"]}
            with self.assertRaises(research.ResearchError):
                research.cached_source(SOURCE, narrowed)

    def test_web_is_explicit_and_no_results_are_fabricated(self):
        with self.assertRaisesRegex(research.ResearchError, "--web"):
            research.research("DayZ", self.config, urls=[SOURCE])
        with patch("research.core_call", return_value={}):
            result = research.research("DayZ", self.config, web=True)
        self.assertFalse(result["web_sources"])
        self.assertTrue(any("No search engine" in str(warning) for warning in result["warnings"]))
        self.assertEqual(result["web_action"], "host_web_search_required")
        self.assertEqual(result["host_web_request"]["action"], "host_web_search_required")
        self.assertLessEqual(len(result["web_queries"]), 3)

    def test_all_excerpts_share_one_budget(self):
        context = {"experience": {"text": "a" * 100, "items": [{"excerpt": "hidden-duplicate", "source": "x"}]},
                   "memory": {"excerpts": [{"text": "b" * 2000, "source": "card.md", "line": 1}]}}
        with patch("research.core_call", return_value=context):
            result = research.research("DayZ", self.config, budget=512)
        self.assertEqual(result["excerpt_chars"], 512)
        self.assertNotIn("hidden-duplicate", json.dumps(result))
        self.assertEqual(len(result["memory"]["experience"]["text"]) +
                         sum(len(item["text"]) for item in result["memory"]["cards"]["excerpts"]), 512)

    def test_core_drift_warning_is_kept(self):
        def fake_core(args, config):
            return {} if args[0] == "context" else {"results": [], "warnings": [{"reason": "source_drift_reindex_required"}]}
        with patch("research.core_call", side_effect=fake_core):
            result = research.research("DayZ", self.config)
        self.assertIn("source_drift_reindex_required", str(result["warnings"]))
        self.assertEqual(result["status"], "no_verified_matches")

    def test_process_timeout_is_bounded(self):
        env = {**__import__("os").environ, "TEMP": str(self.root / "temp")}
        with self.assertRaisesRegex(research.ResearchError, "timeout"):
            research.bounded_process([sys.executable, "-B", "-c", "import time;time.sleep(3)"], 0.1, env)

    def test_control_characters_and_bad_budget_rejected(self):
        for query in ("", "line\nbreak", "a" * 513):
            with self.assertRaises(research.ResearchError):
                research.research(query, self.config)
        with self.assertRaises(research.ResearchError):
            research.research("DayZ", self.config, budget=12001)

    def test_private_json_and_relative_cache_are_rejected(self):
        private = self.root / "Secrets"
        private.mkdir()
        target = private / "data.json"
        target.write_text('{"value":"not-to-read"}', encoding="utf-8")
        with self.assertRaisesRegex(research.ResearchError, "Приватный"):
            research.json_file(target)
        configuration = self.root / "config.json"
        invalid = {**self.config, "cache_root": "relative-cache"}
        configuration.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaisesRegex(research.ResearchError, "абсолютный"):
            research.load_config(configuration)

    def test_explicit_local_cache_path_is_portable(self):
        configuration = self.root / "config.json"
        configuration.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertEqual(research.load_config(configuration)["cache_root"], self.config["cache_root"])

    def test_provenance_metadata_drift_is_not_cache_hit(self):
        with self.allow_dns():
            research.cached_source(SOURCE, self.config, lambda *args: self.source())
            target = next((self.root / "cache/web").glob("*.json"))
            content = json.loads(target.read_text(encoding="utf-8"))
            content["raw_sha256"] = "0" * 64
            target.write_text(json.dumps(content), encoding="utf-8")
            result = research.cached_source(SOURCE, self.config, lambda *args: self.source())
        self.assertEqual(result["cache"], "miss")


if __name__ == "__main__":
    unittest.main()
