"""Synthetic ZIP and real pinned metadata contracts; no network or upstream code."""
import copy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import import_wiki as wiki


class WikiImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bobr-wiki-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "input.zip"
        self.target = self.root / "new-corpus"
        self.payloads = {"en/01-example.md": b"# Example\nScriptRPC test text\n", "README.md": b"# Upstream\n", "LICENCE": b"Separate fixture license\n"}
        self.spec = {"record": {
            "dataset": "WikiFixture", "kind": "community_documentation", "engine": "fixture", "game": "DayZ",
            "channel": "community_wiki_english_not_engine_api_authority", "publisher": "Fixture",
            "commit": "a" * 40, "source_url": "https://github.com/test/fixture", "license_api": {"documentation": "CC-BY-SA-4.0", "code_examples": "MIT"}, "license_paths": ["LICENCE"],
            "files": [{"path": p, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "encoding": "utf-8"} for p, data in self.payloads.items()]
        }}

    def make_zip(self, changes=None, extras=None, omit=()):
        payloads = {**self.payloads, **(changes or {})}
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, data in payloads.items():
                if path not in omit:
                    archive.writestr("source-root/" + path, data)
            for path, data in (extras or {}).items():
                archive.writestr(path, data)

    def assert_rejected_clean(self):
        with self.assertRaises((wiki.ImportError, zipfile.BadZipFile, ValueError)):
            wiki.import_archive(self.archive, self.target, spec=self.spec)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob(".bobr-wiki-*")))

    def test_pinned_manifest_is_107_exact_selection_without_directives(self):
        spec = wiki.load_spec()
        files = spec["record"]["files"]
        self.assertEqual(len(files), 107)
        self.assertEqual(sum(p["path"].startswith("en/") for p in files), 105)
        self.assertTrue(all(p["path"] in ("README.md", "LICENCE") or p["path"].startswith("en/") and p["path"].endswith(".md") for p in files))
        self.assertEqual(spec["record"]["commit"], "e8d971e3941c274befdd6596baba2e5119efb3e4")

    def test_import_exact_bytes_index_provenance_and_license(self):
        self.make_zip(extras={"source-root/.claude/hook.py": b"raise RuntimeError('must never run')"})
        result = wiki.import_archive(self.archive, self.target, spec=self.spec)
        self.assertEqual(result["status"], "imported")
        self.assertTrue(result["verification"]["ok"])
        self.assertFalse(result["upstream_code_executed"])
        self.assertFalse((self.target / "sources/WikiFixture/.claude").exists())
        for relative, raw in self.payloads.items():
            self.assertEqual((self.target / "sources/WikiFixture" / relative).read_bytes(), raw)
        record = json.loads((self.target / "records/WikiFixture.json").read_text("utf-8"))
        self.assertTrue(record["selected_files_sha_verified"])
        self.assertFalse(record["full_archive_git_tree_verified"])
        found = wiki.library.search_index(self.target, "ScriptRPC", "WikiFixture", 2, 1)
        self.assertEqual(found["results"][0]["channel"], self.spec["record"]["channel"])
        self.assertEqual(found["results"][0]["commit"], "a" * 40)
        self.assertIn("/blob/" + "a" * 40, found["results"][0]["file_source_url"])

    def test_wrong_same_size_sha_rejected_and_stage_cleaned(self):
        original = self.payloads["en/01-example.md"]
        self.make_zip(changes={"en/01-example.md": b"X" * len(original)})
        self.assert_rejected_clean()

    def test_missing_selection_rejected(self):
        self.make_zip(omit=("LICENCE",))
        self.assert_rejected_clean()

    def test_wrong_size_rejected(self):
        self.make_zip(changes={"README.md": b"wrong-size"})
        self.assert_rejected_clean()

    def test_traversal_in_unselected_member_also_rejected(self):
        self.make_zip(extras={"source-root/../outside.txt": b"bad"})
        self.assert_rejected_clean()

    def test_absolute_and_drive_backslash_paths_rejected(self):
        for name in ("/root/abs", "C:/bad", "root\\bad", "root//bad", "root/NUL.txt", "root/file. "):
            with self.subTest(name=name):
                with self.assertRaises(wiki.ImportError):
                    wiki.safe_member_name(name)

    def test_duplicate_or_case_collision_rejected(self):
        self.make_zip(extras={"source-root/readme.md": b"case collision"})
        self.assert_rejected_clean()

    def test_unselected_symlink_rejected(self):
        self.make_zip()
        with zipfile.ZipFile(self.archive, "a") as archive:
            info = zipfile.ZipInfo("source-root/unselected-link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../escape")
        self.assert_rejected_clean()

    def test_existing_target_preserved(self):
        self.make_zip()
        self.target.mkdir()
        sentinel = self.target / "owner-data"
        sentinel.write_text("keep", "utf-8")
        with self.assertRaises(wiki.ImportError):
            wiki.import_archive(self.archive, self.target, spec=self.spec)
        self.assertEqual(sentinel.read_text("utf-8"), "keep")

    def test_archive_and_expansion_limits(self):
        self.make_zip()
        for name, value in (("MAX_ARCHIVE_BYTES", 1), ("MAX_EXPANDED_BYTES", 1), ("MAX_MEMBERS", 1), ("MAX_MEMBER_BYTES", 1)):
            with self.subTest(limit=name), patch.object(wiki, name, value):
                self.assert_rejected_clean()

    def test_bad_zip_rejected(self):
        self.archive.write_bytes(b"not a zip")
        self.assert_rejected_clean()

    def test_target_in_repo_or_missing_parent_rejected(self):
        for target in (wiki.ROOT / "not-a-data-root", self.root / "missing/new"):
            with self.subTest(target=target), self.assertRaises(wiki.ImportError):
                wiki.target_path(target)

    def test_cli_does_not_download_without_explicit_mode(self):
        self.make_zip()
        with patch.object(wiki, "download_archive", side_effect=AssertionError("network not allowed")), patch.object(wiki, "load_spec", return_value=self.spec):
            self.assertEqual(wiki.main(["--archive", str(self.archive), "--target", str(self.target)]), 0)

    def test_redirect_host_and_tls_are_restricted(self):
        handler = wiki.PinnedRedirect()
        for url in ("http://codeload.github.com/a", "https://evil.example/a", "https://u:p@codeload.github.com/a", "https://codeload.github.com:444/a"):
            with self.subTest(url=url), self.assertRaises(wiki.ImportError):
                handler.redirect_request(None, None, 302, "", {}, url)

    def test_index_failure_does_not_publish_target(self):
        self.make_zip()
        with patch.object(wiki.library, "build_index", side_effect=ValueError("fixture failure")):
            self.assert_rejected_clean()


if __name__ == "__main__":
    unittest.main()
