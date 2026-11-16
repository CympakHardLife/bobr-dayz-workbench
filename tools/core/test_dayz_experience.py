"""Contract tests for explicit local experience memory; stdlib only."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

from dayz_experience import FileMemory, ExperienceError, ExperienceValidationError, ExperienceLockTimeout


def _writer(root, number, output):
    try:
        memory = FileMemory(root)
        ids = []
        for item in range(3):
            result = memory.record("hearing", f"worker {number}:{item}", "Observed source result", "fixture")
            ids.append(result["id"])
        output.put((True, ids))
    except Exception as exc:
        output.put((False, repr(exc)))


class FileMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="experience_test_", dir=Path(__file__).parent)
        self.root = Path(self.temporary.name).absolute()
        self.memory = FileMemory(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def record(self, **changes):
        values = dict(topic="hearing", title="Голос игрока", body="Проверен обработчик источника.", source="Владелец, тестовый источник")
        values.update(changes)
        return self.memory.record(**values)

    def load_raw(self):
        return json.loads(self.memory.path.read_text(encoding="utf-8"))

    def write_raw(self, data):
        self.memory.directory.mkdir(exist_ok=True)
        self.memory.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_empty_read_has_no_side_effects(self):
        self.assertEqual(self.memory.validate()["records"], 0)
        self.assertEqual(self.memory.recall("hearing")["items"], [])
        self.assertFalse(self.memory.directory.exists())

    def test_persistent_ids_status_and_opaque_source(self):
        result = self.record(source="P:\\source\\does-not-exist.c:17", source_hash="a" * 64)
        fresh = FileMemory(self.root)
        item = fresh.recall("hearing")["items"][0]
        self.assertEqual(item["id"], result["id"])
        self.assertEqual(item["status"], "source_confirmed")
        self.assertNotEqual(item["status"], "runtime_confirmed")
        self.assertEqual(item["source_hash"], "a" * 64)
        self.assertEqual(fresh.validate()["scope"], str(self.root))
        self.assertEqual(fresh.recall("hearing")["trust"], "data_only")

    def test_repeated_titles_append_without_implicit_overwrite(self):
        first, second = self.record(), self.record(body="Новая самостоятельная запись.")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.memory.validate()["active"], 2)
        self.assertEqual({row["id"] for row in self.memory.recall("hearing")["items"]}, {first["id"], second["id"]})

    def test_explicit_supersede_chain_retains_history_and_flags_hypothesis(self):
        first = self.record()
        second = self.record(body="Предположение требует игровой проверки.", status="hypothesis", supersedes=first["id"])
        third = self.record(body="Владелец подтвердил поведение в игре.", status="runtime_confirmed", supersedes=second["id"])
        state = self.memory.validate()
        self.assertEqual((state["records"], state["active"], state["superseded"]), (3, 1, 2))
        self.assertEqual(self.memory.recall("hearing")["items"][0]["id"], third["id"])
        with self.assertRaises(ExperienceValidationError):
            self.record(supersedes=first["id"])
        fourth = self.record(body="Непроверенная идея", status="hypothesis")
        recalled = self.memory.recall("hearing", query="Непроверенная")
        self.assertEqual(recalled["items"][0]["id"], fourth["id"])
        self.assertIn("ГИПОТЕЗА", recalled["text"])
        self.assertIn("hypothesis_is_not_confirmed", recalled["warnings"])

    def test_supersede_rejects_missing_cross_topic_and_cross_scope(self):
        first = self.record()
        with self.assertRaises(ExperienceValidationError):
            self.record(topic="movement", supersedes=first["id"])
        with self.assertRaises(ExperienceValidationError):
            self.record(supersedes="00000000-0000-0000-0000-000000000000")
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(ExperienceValidationError):
            FileMemory(other).record("hearing", "x", "summary", "source", supersedes=first["id"])
        self.assertEqual(self.memory.validate()["active"], 1)

    def test_budget_query_scope_and_utf8(self):
        self.record(body="".join(["Акустика проверяется по исходникам. "] * 100))
        self.record(topic="other", body="Акустика из другой темы.")
        for budget in (0, 1, 80, 200, 1500, 12000):
            result = self.memory.recall("hearing", query="Акустика", budget=budget)
            self.assertLessEqual(len(result["text"]), budget)
            self.assertEqual(result["budget_chars"], budget)
            self.assertLessEqual(len(result["items"]), 8)
        self.assertEqual(self.memory.recall("hearing", "несуществующий_термин")["items"], [])
        self.assertEqual(self.memory.validate()["bytes"], len(self.memory.path.read_bytes()))
        for invalid in (-1, True, 12001):
            with self.assertRaises(ExperienceValidationError):
                self.memory.recall("hearing", budget=invalid)

    def test_query_excerpt_exposes_match_far_from_beginning(self):
        self.record(body="Вводная информация. " * 200 + "ЦЕЛЕВОЙ_СИГНАЛ требует проверки.")
        result = self.memory.recall("hearing", "ЦЕЛЕВОЙ_СИГНАЛ", budget=500)
        self.assertIn("ЦЕЛЕВОЙ_СИГНАЛ", result["text"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"]), 500)

    def test_invalid_inputs_never_touch_snapshot(self):
        self.record()
        before = self.memory.path.read_bytes()
        variants = [dict(topic=""), dict(title="x" * 181), dict(body="x" * 6001), dict(source=""),
                    dict(title="line\nbreak"), dict(body="bad\x00text"), dict(status="confirmed"),
                    dict(status=[]), dict(source_hash="NOT_A_HASH"), dict(supersedes="not-id")]
        for variant in variants:
            with self.subTest(variant=next(iter(variant))):
                with self.assertRaises(ExperienceValidationError):
                    self.record(**variant)
                self.assertEqual(self.memory.path.read_bytes(), before)

    def test_corruption_and_schema_errors_are_not_overwritten(self):
        self.record()
        good = self.load_raw()
        variants = ["{broken", '{"schema_version":1,"schema_version":1}', '{"x":NaN}', '[]']
        malformed = json.loads(json.dumps(good))
        malformed["records"][0]["status"] = []
        variants.append(json.dumps(malformed))
        malformed = json.loads(json.dumps(good))
        malformed["records"][0]["superseded_by"] = malformed["records"][0]["id"]
        variants.append(json.dumps(malformed))
        malformed = json.loads(json.dumps(good))
        malformed["scope"] = "other-project"
        variants.append(json.dumps(malformed))
        for raw in variants:
            with self.subTest(raw=raw[:40]):
                self.memory.path.write_text(raw, encoding="utf-8")
                before = self.memory.path.read_bytes()
                for operation in (self.memory.validate, lambda: self.memory.recall("hearing"), self.record):
                    with self.assertRaises(ExperienceValidationError):
                        operation()
                    self.assertEqual(self.memory.path.read_bytes(), before)

    def test_size_limit_rejects_read_and_append_without_damage(self):
        self.record()
        before = self.memory.path.read_bytes()
        bounded = FileMemory(self.root, limit_bytes=len(before) + 50)
        with self.assertRaises(ExperienceValidationError):
            bounded.record("hearing", "too big", "x" * 200, "source")
        self.assertEqual(self.memory.path.read_bytes(), before)
        tiny = FileMemory(self.root, limit_bytes=128)
        with self.assertRaises(ExperienceValidationError):
            tiny.validate()
        self.assertFalse(list(self.memory.directory.glob("*.tmp")))

    def test_failed_atomic_replace_retains_old_snapshot_and_cleans_temp(self):
        self.record()
        before = self.memory.path.read_bytes()
        with mock.patch("dayz_experience.os.replace", side_effect=PermissionError("fixture")):
            with self.assertRaises(ExperienceError):
                self.record(body="new summary")
        self.assertEqual(self.memory.path.read_bytes(), before)
        self.assertFalse(list(self.memory.directory.glob("*.tmp")))
        self.assertFalse(self.memory.lock_path.exists())

    def test_path_traversal_ads_relative_and_reparse_are_rejected(self):
        for path in ("relative", str(self.root / ".." / "escape"), str(self.root) + ":stream"):
            with self.subTest(path=path), self.assertRaises(ExperienceValidationError):
                FileMemory(path)
        self.memory.directory.mkdir()
        original = Path.lstat
        def reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.memory.directory:
                return type("Reparse", (), {"st_mode": info.st_mode, "st_nlink": info.st_nlink,
                                             "st_file_attributes": 0x400})()
            return info
        with mock.patch.object(Path, "lstat", reparse):
            with self.assertRaises(ExperienceValidationError):
                self.memory.validate()

    def test_real_symlink_or_junction_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, self.memory.directory, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("OS does not allow creating a test symlink")
        try:
            with self.assertRaises(ExperienceValidationError):
                self.record()
            self.assertFalse((outside / "experience.json").exists())
        finally:
            self.memory.directory.unlink()

    def test_lock_wait_is_bounded_and_does_not_steal_stale_lock(self):
        self.memory.directory.mkdir()
        self.memory.lock_path.write_bytes(b"another-owner")
        start = time.monotonic()
        with mock.patch.object(FileMemory, "LOCK_TIMEOUT_SECONDS", 0.12):
            with self.assertRaises(ExperienceLockTimeout):
                self.record()
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(self.memory.lock_path.read_bytes(), b"another-owner")
        self.assertFalse(self.memory.path.exists())

    def test_concurrent_process_writers_do_not_lose_records(self):
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        workers = [context.Process(target=_writer, args=(str(self.root), number, output)) for number in range(4)]
        for process in workers:
            process.start()
        for process in workers:
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join()
                self.fail("writer process exceeded bounded test wait")
            self.assertEqual(process.exitcode, 0)
        results = [output.get(timeout=3) for _ in workers]
        output.close()
        self.assertTrue(all(success for success, _ in results), results)
        ids = [item for _, batch in results for item in batch]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.memory.validate()["records"], 12)
        self.assertEqual({row["id"] for row in self.load_raw()["records"]}, set(ids))


if __name__ == "__main__":
    unittest.main()
