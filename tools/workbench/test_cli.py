"""Приёмочные проверки зарегистрированной поверхности команд и профилей."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import cli
from common import AGENT


class CLITests(unittest.TestCase):
    def test_top_level_help_needs_no_config(self):
        with patch.object(cli, "load_config", side_effect=AssertionError("must not load")):
            code, output = self.invoke(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Workbench", output)

    def test_workdrive_help_needs_no_config_or_action(self):
        with patch.object(cli, "load_config", side_effect=AssertionError("must not load")):
            code, output = self.invoke(["workdrive", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", output)

    def test_missing_config_returns_setup_instruction(self):
        with patch.object(cli, "load_config", side_effect=FileNotFoundError("missing fixture")):
            code, output = self.invoke(["init"])
        self.assertEqual(code, 2)
        value = json.loads(output)
        self.assertEqual(value["status"], "not_configured")
        self.assertEqual(value["bootstrap_next"], "python configure.py")

    def test_health_reports_missing_own_files(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("BOBR_TEST_ROOT", tempfile.gettempdir())) as temporary:
            root = Path(temporary)
            with patch.object(cli, "AGENT", root), patch.object(cli, "roles", return_value=[]):
                value = cli.health({"reports_root": str(root / "reports"), "runs_root": str(root / "runs")})
        self.assertEqual(value["status"], "degraded")
        self.assertIn("nonempty_skills_and_roles", value["missing"])
        self.assertEqual(value["capabilities"]["tools_root"]["status"], "unavailable")

    def test_history_masks_network_addresses(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("BOBR_TEST_ROOT", tempfile.gettempdir())) as temporary:
            cli.history("logs", {"status": "findings", "findings": ["ERROR 198.51.100.27:2302 2001:db8::19"]},
                        {"reports_root": temporary})
            content = next(Path(temporary).glob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn("198.51.100.27", content)
        self.assertNotIn("2001:db8::19", content)

    def invoke(self, arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli.main(arguments)
        return code, stream.getvalue()

    def test_catalog_commands_really_exist(self):
        own = {"commands", "init", "route", "team", "task", "gate", "health", "panel", "workdrive", "editor", "ce-audit", "research"}
        actual = own | cli.CORE_COMMANDS | cli.PIPELINE_COMMANDS | cli.DIAGNOSTIC_COMMANDS
        catalog = {row["name"] for row in cli.catalog()["commands"]}
        self.assertFalse(catalog - actual)
        self.assertFalse(actual - catalog)

    def test_each_command_help_visible_without_actions(self):
        for name in sorted(row["name"] for row in cli.catalog()["commands"]):
            process = subprocess.run([sys.executable, "-B", "-X", "utf8", str(AGENT / "tools/workbench/cli.py"), name, "--help"],
                                     capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(process.returncode, 0, (name, process.stderr, process.stdout))
            self.assertTrue(process.stdout.strip(), name)

    def test_native_roles_parse_and_point_to_skills(self):
        names = set()
        for row in cli.roles():
            profile = tomllib.loads((AGENT / row["profile"]).read_text(encoding="utf-8"))
            self.assertEqual(profile["name"], row["name"])
            self.assertNotIn(profile["name"], names)
            names.add(profile["name"])
            self.assertTrue(profile["developer_instructions"])
            self.assertNotIn("model", profile)
            for skill in row["skills"]:
                self.assertTrue((AGENT / ".agents/skills" / skill / "SKILL.md").is_file(), skill)

    def test_route_audio_ai_rpc_uses_existing_roles(self):
        value = cli.route("Боты не отвечают на голос, проверь RPC админки и звук")
        names = {r["name"] for r in cli.roles()}
        self.assertIn("dayz-audio", {r["role"] for r in value["specialists"]})
        for row in value["specialists"]:
            self.assertIn(row["role"], names)
            self.assertTrue(row["matched"])

    def test_unknown_query_routes_to_existing_architect(self):
        self.assertEqual(cli.route("неизвестная задача 123")["role"], "dayz-architect")

    def test_empty_route_rejected(self):
        with self.assertRaises(ValueError):
            cli.route("  ")

    def test_legacy_slash_alias_matches_canonical(self):
        first = json.loads(self.invoke(["route", "--query", "голос звук"])[1])
        second = json.loads(self.invoke(["/dayz-route", "--query", "голос звук"])[1])
        self.assertEqual(first, second)

    def test_help_submodule_not_swallowed(self):
        for name in ("build", "scaffold", "task", "release"):
            code, output = self.invoke([name, "--help"])
            self.assertEqual(code, 0)
            self.assertIn("usage:", output)

    def test_panel_does_not_include_external_scripts(self):
        root = Path(os.environ.get("BOBR_TEST_ROOT", tempfile.gettempdir())) / "WorkbenchPanel"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            output = Path(temporary) / "panel.html"
            value = cli.panel(output, cli.load_config())
            content = output.read_text(encoding="utf-8")
            self.assertFalse(value["process_control"])
            self.assertNotIn('<script src=', content)
            self.assertNotIn('fetch(', content)
            self.assertIn('lang="ru"', content)

    def test_research_history_does_not_store_query_or_excerpts(self):
        root = Path(os.environ.get("BOBR_TEST_ROOT", tempfile.gettempdir())) / "WorkbenchHistory"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            config = {"reports_root": temporary}
            cli.history("research", {"status": "ok", "query": "PRIVATE_QUESTION_FIXTURE",
                                     "hits": [{"text": "PRIVATE_EXCERPT_FIXTURE"}], "excerpt_chars": 20}, config)
            reports = list(Path(temporary).glob("*.json"))
            self.assertEqual(len(reports), 1)
            content = reports[0].read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_QUESTION_FIXTURE", content)
            self.assertNotIn("PRIVATE_EXCERPT_FIXTURE", content)


if __name__ == "__main__":
    unittest.main()
