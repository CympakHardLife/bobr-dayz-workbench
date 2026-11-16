"""Проверки восстановления, порогов и отсутствия сохранения входных событий."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from checkpoint_hook import FILES, process


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Проверенные договорённости\n", "utf-8")

    def event(self, name, now, **extra):
        return process(self.root, dict(hook_event_name=name, **extra), now)

    def state(self):
        return json.loads((self.root / "memory/checkpoints/state.json").read_text("utf-8"))

    def test_restore_bytes_and_manifest(self):
        self.event("Manual", 1000)
        snapshot = self.root / "memory/checkpoints" / self.state()["snapshot"]
        manifest = json.loads((snapshot / "manifest.json").read_text("utf-8"))
        for name in FILES:
            raw = (snapshot / name).read_bytes()
            self.assertEqual(raw, (self.root / name).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest["sha256"][name])

    def test_no_duplicate_snapshot(self):
        self.event("Manual", 1000)
        before = self.state()["snapshot"]
        self.event("Stop", 1001)
        self.assertEqual(before, self.state()["snapshot"])

    def test_twenty_calls(self):
        self.event("Manual", 1000)
        for index in range(19):
            self.assertEqual(self.event("PostToolUse", 1001 + index), {})
        self.assertIn("hookSpecificOutput", self.event("PostToolUse", 1020))

    def test_time_threshold(self):
        self.event("Manual", 1000)
        self.assertEqual(self.event("PostToolUse", 1899), {})
        self.assertIn("hookSpecificOutput", self.event("PostToolUse", 1900))

    def test_no_transcript_or_tool_data(self):
        marker = "DO_NOT_STORE_INPUT"
        self.event("Stop", 1000, transcript_path=marker, tool_input=marker,
                   last_assistant_message=marker, tool_response=marker)
        for path in (self.root / "memory/checkpoints").rglob("*"):
            if path.is_file():
                self.assertNotIn(marker.encode(), path.read_bytes())

    def test_secret_rejected(self):
        (self.root / ".ai-rules.md").write_text("api_key=" + "x" * 32, "utf-8")
        with self.assertRaises(ValueError):
            self.event("Manual", 1000)
        self.assertFalse((self.root / "memory/checkpoints/state.json").exists())

    def test_outside_cwd_ignored(self):
        self.assertEqual(self.event("Manual", 1000, cwd=str(self.root.parent)), {})
        self.assertFalse((self.root / "memory/checkpoints").exists())

    def test_unknown_event_ignored(self):
        self.assertEqual(self.event("PermissionRequest", 1000), {})

    def test_changed_memory_snapshot_on_stop(self):
        self.event("Manual", 1000)
        previous = self.state()["snapshot"]
        (self.root / "memory/CURRENT.md").write_text("Новый результат", "utf-8")
        self.event("Stop", 1001)
        self.assertNotEqual(previous, self.state()["snapshot"])

    def test_topic_card_is_snapshotted_on_stop(self):
        self.event("Manual", 1000)
        previous = self.state()["snapshot"]
        (self.root / "memory/cards/dayzmods-architecture.md").write_text("Новый результат темы", "utf-8")
        self.event("Stop", 1001)
        self.assertNotEqual(previous, self.state()["snapshot"])
        snapshot = self.root / "memory/checkpoints" / self.state()["snapshot"]
        self.assertEqual((snapshot / "memory/cards/dayzmods-architecture.md").read_text("utf-8"), "Новый результат темы")

    def test_source_path_escape(self):
        source = self.root / "memory/PROFILE.md"
        source.unlink()
        try:
            source.symlink_to(self.root.parent / "other")
        except OSError:
            self.skipTest("ОС не разрешает создание symlink")
        with self.assertRaises(ValueError):
            self.event("Manual", 1000)


if __name__ == "__main__":
    unittest.main()
