"""Проверки адресной загрузки, ограничений и границ источников памяти."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import context_router as router


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "tools/memory").mkdir(parents=True)
        self.summary = self.root / "summary.md"
        self.detail = self.root / "detail.md"
        self.summary.write_text("# Текущая задача\nДокументы: 500 рублей.\n", encoding="utf-8")
        self.detail.write_text("# Архив\nДругие сведения.\nДокументы: прежняя цена.\n", encoding="utf-8")
        self.routes = {"version": 1, "topics": {"trader": {
            "description": "Трейдер", "summary": "summary.md", "sources": ["detail.md"]}}}
        self.write_routes()

    def write_routes(self):
        (self.root / "tools/memory/routes.json").write_text(json.dumps(self.routes), encoding="utf-8")

    def test_plan_reads_no_knowledge(self):
        original = router.read_text
        with patch.object(router, "read_text", wraps=original) as read:
            result = router.retrieve(self.root, "trader")
        self.assertEqual([call.args[1] for call in read.call_args_list], ["tools/memory/routes.json"])
        self.assertEqual(result["text_chars"], 0)

    def test_summary_does_not_load_details(self):
        with patch.object(router, "read_text", wraps=router.read_text) as read:
            result = router.retrieve(self.root, "trader", read=True)
        self.assertNotIn("detail.md", [call.args[1] for call in read.call_args_list])
        self.assertIn("500 рублей", result["excerpts"][0]["text"])

    def test_query_returns_source_lines(self):
        result = router.retrieve(self.root, "trader", query="документы")
        self.assertEqual({item["source"] for item in result["excerpts"]}, {"summary.md", "detail.md"})
        self.assertTrue(all(item["line"] > 0 for item in result["excerpts"]))

    def test_text_budget_is_strict(self):
        self.summary.write_text("Ж" * 2000, encoding="utf-8")
        result = router.retrieve(self.root, "trader", read=True, budget=256)
        self.assertEqual(sum(len(item["text"]) for item in result["excerpts"]), 256)
        self.assertTrue(result["excerpts"][0]["truncated"])

    def test_path_traversal_and_absolute_paths_rejected(self):
        for path in ("../outside.md", str(self.summary)):
            with self.subTest(path=path), self.assertRaises(ValueError):
                router.checked_path(self.root, path)

    def test_unregistered_secret_is_never_read(self):
        (self.root / ".env").write_text("PRIVATE_SENTINEL", encoding="utf-8")
        result = router.retrieve(self.root, "trader", query="PRIVATE_SENTINEL")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
        with self.assertRaises(ValueError):
            router.checked_path(self.root, ".env")

    def test_missing_or_large_source_is_reported(self):
        self.summary.write_bytes(b"X" * (router.MAX_FILE_BYTES + 1))
        result = router.retrieve(self.root, "trader", read=True)
        self.assertEqual(result["excerpts"], [])
        self.assertTrue(result["warnings"])
        self.summary.unlink()
        self.assertTrue(router.retrieve(self.root, "trader", read=True)["warnings"])

    def test_unknown_topic_and_empty_query_rejected(self):
        with self.assertRaises(ValueError):
            router.retrieve(self.root, "missing")
        with self.assertRaises(ValueError):
            router.retrieve(self.root, "trader", query=" ")

    def test_symlink_is_rejected(self):
        link = self.root / "link.md"
        try:
            link.symlink_to(self.summary)
        except OSError:
            self.skipTest("ОС не разрешает создание symlink")
        with self.assertRaises(ValueError):
            router.checked_path(self.root, "link.md")


if __name__ == "__main__":
    unittest.main()
