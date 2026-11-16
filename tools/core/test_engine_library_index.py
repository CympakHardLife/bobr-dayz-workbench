"""Безопасные проверки индекса на искусственной локальной базе."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import engine_library_index as library


class LibraryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="engine-library-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "records").mkdir()
        self.make_dataset("rv3", "ai.sqf", b"// archive sample\nFSM dangerous trigger\n", "utf-8")
        self.make_dataset("dayz", "actor.c", "class Actor\n{\n    void Speak() { }\n};\nГолос игрока\n".encode("utf-8"), "utf-8")

    def make_dataset(self, dataset, filename, data, encoding):
        directory = self.root / "sources" / dataset
        directory.mkdir(parents=True)
        (directory / filename).write_bytes(data)
        record = {
            "dataset": dataset,
            "engine": "Real Virtuality 3" if dataset == "rv3" else "DayZ Enforce",
            "game": "Arma 2" if dataset == "rv3" else "DayZ",
            "kind": "test_fixture",
            "source_url": "https://github.com/example/repository",
            "commit": "a" * 40,
            "license_api": {"license": {"spdx_id": "MIT"}},
            "files": [{"path": filename, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "encoding": encoding}],
        }
        (self.root / "records" / f"{dataset}.json").write_text(json.dumps(record), "utf-8")

    def test_dataset_boundary_provenance_and_readonly_search(self):
        manifest = library.build_index(self.root)
        before = library.digest_file(self.root / "index.sqlite")
        result = library.search_index(self.root, "Actor", "dayz", 5, 1)
        self.assertEqual(result["count"], 1)
        hit = result["results"][0]
        self.assertEqual(hit["game"], "DayZ")
        self.assertEqual(hit["snippets"][0]["line"], 1)
        self.assertIn("/blob/" + "a" * 40 + "/actor.c", hit["file_source_url"])
        self.assertEqual(library.search_index(self.root, "Actor", "rv3", 5, 1)["count"], 0)
        self.assertEqual(library.search_index(self.root, "Голос", None, 5, 0)["count"], 1)
        self.assertEqual(before, library.digest_file(self.root / "index.sqlite"))
        self.assertTrue(library.verify_index(self.root)["ok"])
        self.assertEqual(manifest["counts"]["indexed_files"], 2)

    def test_like_backend_and_literal_query(self):
        manifest = library.build_index(self.root, force_like=True)
        self.assertEqual(manifest["backend"], "like")
        self.assertEqual(library.search_index(self.root, "голос", "dayz", 5, 0)["count"], 1)
        self.assertEqual(library.search_index(self.root, "' OR 1=1 --", None, 5, 0)["count"], 0)
        self.assertEqual(library.search_index(self.root, "Actor", "dayz' OR 1=1 --", 5, 0)["count"], 0)

    def test_file_drift_and_new_file_detected(self):
        library.build_index(self.root)
        (self.root / "sources/dayz/actor.c").write_text("changed", "utf-8")
        (self.root / "sources/dayz/new.c").write_text("new", "utf-8")
        verified = library.verify_index(self.root)
        self.assertFalse(verified["ok"])
        self.assertEqual({item["kind"] for item in verified["mismatches"]}, {"file_drift", "new_file"})

    def test_record_hash_drift_detected(self):
        library.build_index(self.root)
        path = self.root / "records/dayz.json"
        record = json.loads(path.read_text("utf-8"))
        record["game"] = "not DayZ"
        path.write_text(json.dumps(record), "utf-8")
        self.assertIn("record_sha256", {item["kind"] for item in library.verify_index(self.root)["mismatches"]})

    def test_binary_and_lfs_not_promoted_to_source(self):
        (self.root / "sources/dayz/binary.txt").write_bytes(b"a\0b")
        (self.root / "sources/dayz/large.txt").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:abcd\nsize 100\n", "utf-8")
        manifest = library.build_index(self.root)
        self.assertEqual(manifest["skipped_files"], 2)
        self.assertTrue(library.verify_index(self.root)["ok"])

    def test_encoding_and_record_mismatch(self):
        self.make_dataset("legacy", "readme.txt", "café".encode("cp1252"), "cp1252")
        library.build_index(self.root)
        self.assertEqual(library.search_index(self.root, "café", "legacy", 5, 0)["count"], 1)
        (self.root / "sources/legacy/readme.txt").write_bytes(b"altered")
        with self.assertRaisesRegex(ValueError, "SHA"):
            library.build_index(self.root)

    def test_parent_path_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            library.check_plain(self.root / "sources" / ".." / ".." / "outside.txt", self.root)
        link = self.root / "sources/dayz/link.c"
        try:
            link.symlink_to(self.root / "sources/rv3/ai.sqf")
        except OSError:
            return
        with self.assertRaisesRegex(ValueError, "reparse"):
            library.build_index(self.root)


if __name__ == "__main__":
    unittest.main()
