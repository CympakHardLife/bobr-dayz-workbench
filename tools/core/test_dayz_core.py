"""Изолированные проверки контекста, кеша и границ собственного ядра."""

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

import dayz_core
import engine_library_index as library


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bobr_core_", dir=Path(__file__).resolve().parent)
        self.root = Path(self.temporary.name)
        self.agent = self.root / "Agent"
        self.catalog = self.root / "Library"
        (self.agent / "tools/memory").mkdir(parents=True)
        (self.agent / "memory").mkdir()
        source_router = Path(dayz_core.__file__).resolve().parents[1] / "memory/context_router.py"
        shutil.copyfile(source_router, self.agent / "tools/memory/context_router.py")
        self.memory_path = self.agent / "memory/ai.md"
        self.memory_path.write_text("# AI\nШум и поиск. Проверять current API.\n", encoding="utf-8")
        (self.agent / "tools/memory/routes.json").write_text(json.dumps({"version": 1, "topics": {"ai": {"description": "AI", "summary": "memory/ai.md", "sources": []}}}), encoding="utf-8")
        (self.catalog / "records").mkdir(parents=True)
        self.make_dataset("Vanilla", "DayZ", "class PlayerBase\n{\n    void HearNoise() {}\n}\n")
        self.make_dataset("OtherGame", "Arma Reforger", "class SCR_Player\n{\n    void HearNoise() {}\n}\n")
        library.build_index(self.catalog)
        self.config_path = self.root / "config.json"
        self.config = {
            "schema_version": 1,
            "agent_root": str(self.agent),
            "storage_drive": self.root.drive or self.root.anchor,
            "cache": {"root": str(self.root / "Cache"), "ttl_seconds": 3600, "max_entries": 2, "max_payload_bytes": 65536},
            "scopes": {
                "dayz": {"root": str(self.catalog), "game": "DayZ", "datasets": ["Vanilla"]},
                "reforger": {"root": str(self.catalog), "game": "Reforger", "datasets": ["OtherGame"]},
            },
            "backends": {"local": "active", "chroma": "analysed_disabled"},
        }
        self.save_config()
        self.core = dayz_core.Core(self.config_path)

    def tearDown(self):
        self.temporary.cleanup()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def make_dataset(self, name, game, content):
        target = self.catalog / "sources" / name / "player.c"
        target.parent.mkdir(parents=True)
        raw = content.encode()
        target.write_bytes(raw)
        record = {"dataset": name, "kind": "test", "engine": "test", "game": game, "channel": "test", "commit": "1" * 40, "source_url": "https://github.com/test/" + name, "publisher": "test", "files": [{"path": "player.c", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "encoding": "utf-8"}]}
        (self.catalog / "records" / (name + ".json")).write_text(json.dumps(record), encoding="utf-8")

    def test_cache_reuses_only_verified_matching_sources(self):
        first = self.core.search("dayz", "HearNoise")
        second = self.core.search("dayz", "HearNoise")
        self.assertEqual(first["cache"], "miss")
        self.assertEqual(second["cache"], "hit_verified")
        self.assertEqual(first["results"], second["results"])

    def test_changed_source_invalidates_cache_and_stale_index(self):
        self.core.search("dayz", "HearNoise")
        path = self.catalog / "sources/Vanilla/player.c"
        path.write_text("class Changed {}", encoding="utf-8")
        result = self.core.search("dayz", "HearNoise")
        self.assertEqual(result["cache"], "miss")
        self.assertFalse(result["results"])
        self.assertTrue(any(row.get("reason") == "source_drift_reindex_required" for row in result["warnings"]))

    def test_record_drift_is_not_silently_accepted(self):
        path = self.catalog / "records/Vanilla.json"
        record = json.loads(path.read_text())
        record["commit"] = "2" * 40
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Record"):
            self.core.search("dayz", "HearNoise")

    def test_dayz_and_reforger_are_separate_scopes(self):
        dayz = self.core.search("dayz", "HearNoise")
        reforger = self.core.search("reforger", "HearNoise")
        self.assertEqual([row["dataset"] for row in dayz["results"]], ["Vanilla"])
        self.assertEqual([row["dataset"] for row in reforger["results"]], ["OtherGame"])

    def test_excerpt_budget_is_enforced_across_context(self):
        raw = ("class PlayerBase\n{\n    void HearNoise() {} //" + "x" * 2000 + "\n}\n").encode()
        path = self.catalog / "sources/Vanilla/player.c"
        path.write_bytes(raw)
        record_path = self.catalog / "records/Vanilla.json"
        record = json.loads(record_path.read_text())
        record["files"][0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        record_path.write_text(json.dumps(record), encoding="utf-8")
        library.build_index(self.catalog)
        result = self.core.context("ai", "HearNoise", budget=512)
        self.assertLessEqual(result["excerpt_chars"], 512)
        self.assertEqual(result["excerpt_chars"], len(result["experience"]["text"]) + result["memory"]["text_chars"] + result["code"]["excerpt_chars"])

    def test_memory_edit_is_visible_immediately(self):
        self.core.context("ai", "Шум")
        self.memory_path.write_text("# AI\nШум: новая проверенная реакция.\n", encoding="utf-8")
        result = self.core.context("ai", "Шум")
        self.assertIn("новая", result["memory"]["excerpts"][0]["text"])

    def test_reviewed_experience_is_recalled_and_superseded(self):
        old = self.core.remember("ai", "Шум", "Старый вывод", "review fixture", "hypothesis")
        self.core.remember("ai", "Шум", "Новый вывод по source", "new fixture", "source_confirmed", old["id"])
        result = self.core.context("ai", "Шум", budget=1000)
        self.assertIn("Новый", result["experience"]["text"])
        self.assertNotIn("Старый", result["experience"]["text"])
        self.assertLessEqual(result["excerpt_chars"], 1000)
        self.assertNotIn("excerpt", result["experience"]["items"][0])

    def test_missing_external_library_keeps_memory_context(self):
        self.config["scopes"]["dayz"]["root"] = str(self.root / "Absent")
        self.save_config()
        core = dayz_core.Core(self.config_path)
        result = core.context("ai", "Шум")
        self.assertTrue(result["memory"]["excerpts"])
        self.assertIn("code_unavailable", result)

    def test_cache_count_bounded_and_clear_preserves_sources(self):
        for query in ("HearNoise", "PlayerBase", "class"):
            self.core.search("dayz", query)
        self.assertLessEqual(self.core.cache_action("status")["entries"], 2)
        self.core.cache_action("clear")
        self.assertEqual(self.core.cache_action("status")["entries"], 0)
        self.assertTrue(self.memory_path.is_file())
        self.assertTrue((self.catalog / "sources/Vanilla/player.c").is_file())

    def test_expired_cache_is_refreshed(self):
        self.core.search("dayz", "HearNoise")
        connection = sqlite3.connect(self.root / "Cache/retrieval.sqlite")
        connection.execute("UPDATE entries SET created=0")
        connection.commit()
        connection.close()
        self.assertEqual(self.core.search("dayz", "HearNoise")["cache"], "miss")

    def test_registered_sources_reject_traversal_and_foreign_watch(self):
        for relative in ("../outside", str(self.root / "outside"), "memory/ai.md:stream"):
            with self.assertRaises(ValueError):
                dayz_core.within(self.agent, relative)
        self.assertFalse(self.core.watches_valid([{"owner": "memory", "path": "Secrets/password.json", "sha256": "0" * 64}]))
        self.assertFalse(self.core.watches_valid([{"owner": "library", "scope": "dayz", "path": "sources/OtherGame/player.c", "sha256": "0" * 64}]))

    def test_symlink_root_rejected_when_supported(self):
        link = self.root / "Alias"
        try:
            link.symlink_to(self.catalog, target_is_directory=True)
        except OSError:
            self.skipTest("symlink unavailable on this Windows account")
        with self.assertRaises(ValueError):
            dayz_core.canonical(link)

    def test_cache_drive_mismatch_rejected(self):
        self.config["storage_drive"] = "other-drive:"
        self.save_config()
        with self.assertRaisesRegex(ValueError, "диска"):
            dayz_core.Core(self.config_path)

    def test_missing_scope_and_invalid_budgets_rejected(self):
        for budget in (0, 255, 12001):
            with self.assertRaises(ValueError):
                self.core.search("dayz", "class", budget=budget)
        with self.assertRaises(ValueError):
            self.core.search("unknown", "class")

    def test_malformed_config_is_rejected_before_import(self):
        for value in (None, [], {"schema_version": 1}):
            self.config_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                dayz_core.Core(self.config_path)

    def test_cache_limits_cannot_disable_bounds(self):
        for value in (-1, 0, True, None, 100000):
            self.config["cache"]["max_entries"] = value
            self.save_config()
            with self.assertRaises(ValueError):
                dayz_core.Core(self.config_path)

    def test_forged_cached_empty_watches_are_not_verified(self):
        self.core.search("dayz", "HearNoise")
        connection = sqlite3.connect(self.root / "Cache/retrieval.sqlite")
        payload = json.loads(connection.execute("SELECT payload FROM entries").fetchone()[0])
        payload["_watched"] = []
        payload["results"][0]["path"] = "outside-root"
        payload["results"][0]["snippets"][0]["text"] = "x" * 15000
        connection.execute("UPDATE entries SET payload=?", (json.dumps(payload),))
        connection.commit()
        connection.close()
        result = self.core.search("dayz", "HearNoise")
        self.assertEqual(result["cache"], "miss")
        self.assertNotEqual(result["results"][0]["path"], "outside-root")

    def test_cached_text_must_match_source_not_only_watch_hash(self):
        self.core.search("dayz", "HearNoise")
        connection = sqlite3.connect(self.root / "Cache/retrieval.sqlite")
        payload = json.loads(connection.execute("SELECT payload FROM entries").fetchone()[0])
        payload["results"][0]["snippets"][0]["text"] = "fabricated"
        payload["excerpt_chars"] = 10
        connection.execute("UPDATE entries SET payload=?", (json.dumps(payload),))
        connection.commit()
        connection.close()
        self.assertEqual(self.core.search("dayz", "HearNoise")["cache"], "miss")

    def test_cached_provenance_labels_cannot_be_forged(self):
        self.core.search("dayz", "HearNoise")
        connection = sqlite3.connect(self.root / "Cache/retrieval.sqlite")
        payload = json.loads(connection.execute("SELECT payload FROM entries").fetchone()[0])
        payload["results"][0].update(dataset="OtherGame", game="Arma Reforger", commit="9" * 40, source_url="https://unrelated.invalid")
        connection.execute("UPDATE entries SET payload=?", (json.dumps(payload),))
        connection.commit()
        connection.close()
        result = self.core.search("dayz", "HearNoise")
        self.assertEqual(result["cache"], "miss")
        self.assertEqual(result["results"][0]["dataset"], "Vanilla")

    def test_source_drift_same_size_and_mtime_still_invalidates(self):
        import os
        self.core.search("dayz", "HearNoise")
        path = self.catalog / "sources/Vanilla/player.c"
        status = path.stat()
        path.write_bytes(path.read_bytes().replace(b"HearNoise", b"FakeNoise"))
        os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))
        self.assertFalse(self.core.search("dayz", "HearNoise")["results"])

    def test_conditional_response_saves_repeated_text_but_tracks_changes(self):
        first = dayz_core.conditional_response(self.core.search("dayz", "HearNoise"))
        second = dayz_core.conditional_response(self.core.search("dayz", "HearNoise"), first["response_id"])
        self.assertTrue(second["unchanged"])
        self.assertNotIn("results", second)
        changed = dayz_core.conditional_response({"different": "data"}, first["response_id"])
        self.assertFalse(changed["unchanged"])

    def test_oversized_drift_source_is_not_read_unbounded(self):
        self.core.search("dayz", "HearNoise")
        path = self.catalog / "sources/Vanilla/player.c"
        path.write_bytes(b"x" * 1024)
        original = dayz_core.MAX_SOURCE_BYTES
        dayz_core.MAX_SOURCE_BYTES = 512
        try:
            with self.assertRaisesRegex(ValueError, "лимит"):
                self.core.search("dayz", "HearNoise")
        finally:
            dayz_core.MAX_SOURCE_BYTES = original

    def test_static_gate_plan_has_no_game_build_or_network(self):
        gates = self.agent / ".agents/skills/dayz-ai-engineering/scripts"
        gates.mkdir(parents=True)
        for name in dayz_core.GATES:
            (gates / name).write_text("# test placeholder; never executed\n", encoding="utf-8")
        symbols = self.root / "symbols.sqlite"
        symbols.write_bytes(b"fixture")
        result = self.core.check(self.catalog / "sources/Vanilla", self.catalog / "sources/Vanilla", symbols)
        self.assertEqual(result["mode"], "plan")
        self.assertEqual(len(result["commands"]), 6)
        self.assertFalse(result["results"])
        self.assertTrue(all(command[-1] == "--strict" for command in result["commands"]))

    def test_empty_or_malformed_release_manifest_cannot_claim_verified(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        directory = self.agent / "core"
        directory.mkdir()
        for value in ({}, [], None):
            (directory / "CORE_MANIFEST.json").write_text(json.dumps({"files": value}), encoding="utf-8")
            with patch("dayz_core.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="{}", stderr="")):
                with self.assertRaisesRegex(ValueError, "CORE_MANIFEST"):
                    self.core.verify()


if __name__ == "__main__":
    unittest.main()
