"""Проверки реальных файлов и отрицательных сценариев Workbench."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import cli
import diagnostics as d
from common import checked_path, redact


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ.get("BOBR_TEST_ROOT", tempfile.gettempdir())) / "WorkbenchDiagnostics"
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_local_missing_and_commented_resource(self):
        self.write("$PBOPREFIX$", "Own")
        self.write("config.cpp", '// "Own/no.ogg"\nclass X { sample="Own/real.ogg"; };')
        value = d.resources(self.root)
        self.assertEqual(value["status"], "fail")
        self.assertEqual([r["reference"] for r in value["findings"]], ["Own/real.ogg"])

    def test_external_reference_coverage(self):
        self.write("config.cpp", 'class X { model="DZ/weapons/a.p3d"; };')
        value = d.resources(self.root)
        self.assertEqual(value["status"], "pass")
        self.assertEqual(value["external_references"], ["DZ/weapons/a.p3d"])
        self.assertIn("external_prefix_resolution", value["skipped"])

    def test_audio_header_mismatch(self):
        self.write("fake.ogg", "RIFFnotogg")
        value = d.resources(self.root)
        self.assertEqual(value["findings"][0]["code"], "resource_header")

    def test_docs_not_runtime_resource(self):
        self.write("README.md", 'Example: "Own/missing.ogg"')
        self.assertEqual(d.resources(self.root)["status"], "pass")

    def test_layout_negative_size_and_braces(self):
        path = self.write("menu.layout", 'FrameWidgetClass Root\n{\n size 0.5 -1\n')
        value = d.layout_audit(path)
        self.assertEqual(value["status"], "fail")
        self.assertEqual({r["code"] for r in value["findings"]}, {"unbalanced_braces", "non_positive_size"})

    def test_empty_layout_scope_not_run(self):
        self.assertEqual(d.layout_audit(self.root)["status"], "not_run")

    def test_api_overload_and_allman(self):
        self.write("source/test.c", 'class A\n{\n void Go(int value)\n {\n }\n void Go(string value) {}\n};\n')
        target = self.root / "snapshot.json"
        d.api_snapshot(self.root / "source", target, "fixture")
        methods = [r for r in json.loads(target.read_text(encoding="utf-8"))["records"] if r["kind"] == "method"]
        self.assertEqual(len(methods), 2)
        self.assertNotEqual(methods[0]["declaration"], methods[1]["declaration"])

    def test_api_content_and_hash_same_read(self):
        path = self.write("source/test.c", 'class Old {}')
        original = d.source_bytes
        def mutate(value):
            raw = original(value)
            path.write_text("class New {}", encoding="utf-8")
            return raw
        target = self.root / "snapshot.json"
        with patch.object(d, "source_bytes", side_effect=mutate):
            d.api_snapshot(path.parent, target, "fixture")
        value = json.loads(target.read_text(encoding="utf-8"))
        import hashlib
        self.assertEqual(value["files"]["test.c"], hashlib.sha256(b"class Old {}").hexdigest())
        self.assertIn("Old", value["records"][0]["declaration"])

    def test_api_body_change_visible_without_signature_change(self):
        source = self.write("source/a.c", "class A { };\nvoid Go() { int x = 1; }\n")
        before, after = self.root / "before.json", self.root / "after.json"
        d.api_snapshot(source.parent, before, "before")
        source.write_text("class A { };\nvoid Go() { int x = 2; }\n", encoding="utf-8")
        d.api_snapshot(source.parent, after, "after")
        value = d.api_diff(before, after)
        self.assertTrue(value["needs_review"])
        self.assertEqual(value["removed"], [])
        self.assertEqual(value["added"], [])
        self.assertEqual(value["changed_files"], ["a.c"])

    def test_snapshot_no_overwrite(self):
        self.write("source/a.c", "class A {}")
        out = self.write("old.json", "preserved")
        with self.assertRaises(ValueError):
            d.api_snapshot(self.root / "source", out, "x")
        self.assertEqual(out.read_text(), "preserved")

    def test_log_partial_compile_line_is_retained(self):
        path = self.write("session.log", "x" * 150 + "\nCan't com")
        cursor = self.root / "cursor.json"
        first = d.logs(path, cursor)
        self.assertEqual(first["total_matches"], 0)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("pile World\n")
        second = d.logs(path, cursor)
        self.assertEqual(second["total_matches"], 1)
        self.assertIn("Can't compile World", second["findings"][0])
        self.assertEqual(d.logs(path, cursor)["total_matches"], 0)

    def test_log_json_and_player_ids_masked(self):
        path = self.write("session.log", 'ERROR {"token":"FAKE_CREDENTIAL_123456"} id=76561198000000000\n')
        value = d.logs(path)
        serialized = json.dumps(value)
        self.assertNotIn("FAKE_CREDENTIAL", serialized)
        self.assertNotIn("76561198000000000", serialized)

    def test_empty_log_cursor_then_rewrite_resets_offset(self):
        path = self.write("session.log", "")
        cursor = self.root / "cursor.json"
        d.logs(path, cursor)
        path.write_text("ERROR OLD\n", encoding="utf-8")
        self.assertIn("ERROR OLD", d.logs(path, cursor)["findings"])
        path.write_text("ERROR NEW FIRST\nmore data\n", encoding="utf-8")
        value = d.logs(path, cursor)
        self.assertEqual(value["bytes_from"], 0)
        self.assertIn("ERROR NEW FIRST", value["findings"])

    def test_log_network_addresses_masked(self):
        path = self.write("session.log", "ERROR 198.51.100.27:2302 [2001:db8::19]:2302\n")
        content = json.dumps(d.logs(path))
        self.assertNotIn("198.51.100.27", content)
        self.assertNotIn("2001:db8::19", content)

    def test_log_cannot_overwrite_itself(self):
        path = self.write("session.json", "{}")
        with self.assertRaises(ValueError):
            d.logs(path, path)

    def test_release_sha_and_crc_roundtrip(self):
        path = self.write("README.md", "Release fixture")
        output = self.root / "release.zip"
        self.assertEqual(d.release([path], output)["status"], "pass")
        with zipfile.ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("README.md"), b"Release fixture")
            self.assertIn("README.md", json.loads(archive.read("MANIFEST.json"))["files"])

    def test_release_json_xml_secrets_rejected(self):
        for content in ('{"token":"FAKE_RELEASE_TOKEN_12345"}', '<password>FAKE_RELEASE_TOKEN_12345</password>'):
            path = self.write("settings.json", content)
            with self.assertRaises(ValueError):
                d.release([path], self.root / "release.zip")

    def test_release_manifest_windows_collision(self):
        path = self.write("manifest.json", "{}")
        with self.assertRaises(ValueError):
            d.release([path], self.root / "release.zip")

    def test_release_duplicate_basename(self):
        a, b = self.write("a/help.md", "a"), self.write("b/HELP.md", "b")
        with self.assertRaises(ValueError):
            d.release([a, b], self.root / "release.zip")

    def test_release_reads_pbo_and_rejects_embedded_private_field(self):
        import pipeline
        source = self.root / "pbo-source"
        source.mkdir()
        (source / "config.cpp").write_text('class CfgPatches {};', encoding="utf-8")
        (source / "settings.json").write_text('{"token":"FAKE_PRIVATE_FIXTURE_123456"}', encoding="utf-8")
        pbo = self.root / "own.pbo"
        with patch.object(pipeline, "runtime_config", return_value={"approved_write_roots": [str(self.root)]}):
            pipeline.write_uncompressed_pbo(source, pbo, "Own")
        with self.assertRaises(ValueError):
            d.release([pbo], self.root / "release.zip")

    def test_release_rejects_fake_pbo(self):
        pbo = self.write("fake.pbo", "not a PBO")
        with self.assertRaises(ValueError):
            d.release([pbo], self.root / "release.zip")

    def test_private_path_and_parent_escape_rejected(self):
        for value in (self.root / "Secrets/x.json", self.root / "a/../other", self.root / "secret.biprivatekey"):
            with self.assertRaises(ValueError):
                checked_path(value)

    def test_session_server_only_argument(self):
        config = {"game_root": str(self.root)}
        value = d.session_plan(self.root, "server", config)
        self.assertIsNone(value["client_mod_argument"])
        self.assertTrue(value["server_mod_argument"].startswith("-serverMod="))

    def test_editor_and_texture_default_do_not_start_process(self):
        tools = self.root / "Tools"
        fake = tools / "Bin/ImageToPAA/ImageToPAA.exe"
        fake.parent.mkdir(parents=True)
        fake.write_bytes(b"fixture")
        image = self.write("image_co.tga", "fixture")
        with patch.object(d.subprocess, "run", side_effect=AssertionError("must not run")):
            value = d.texture(image, self.root / "image_co.paa", tools)
        self.assertEqual(value["status"], "plan")


if __name__ == "__main__":
    unittest.main()
