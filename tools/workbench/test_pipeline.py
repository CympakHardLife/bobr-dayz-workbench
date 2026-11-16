"""Проверки контракта pipeline на изолированных файлах; игра не запускается."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pipeline


TEST_ROOT = Path(os.environ["BOBR_TEST_ROOT"]).absolute() if os.environ.get("BOBR_TEST_ROOT") else None


def write_pbo(path, entries, prefix="TestMod", *, checksum=True, properties=None):
    header = b"\0" + struct.pack("<5I", pipeline.VERS, 0, 0, 0, 0)
    values = properties if properties is not None else {"prefix": prefix}
    for key, value in values.items():
        header += key.encode("utf-8") + b"\0" + value.encode("utf-8") + b"\0"
    header += b"\0"
    payload = b""
    for name, data, packing in entries:
        header += name.encode("utf-8") + b"\0" + struct.pack("<5I", packing, len(data), 0, 0, len(data))
        payload += data
    header += b"\0" + b"\0" * 20
    contents = header + payload
    if checksum:
        contents += b"\0" + hashlib.sha1(contents).digest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


class PipelineTests(unittest.TestCase):
    def setUp(self):
        if TEST_ROOT:
            TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=TEST_ROOT)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tools = self.root / "tools"
        (self.tools / "Bin/AddonBuilder").mkdir(parents=True)
        (self.tools / "Bin/AddonBuilder/AddonBuilder.exe").write_bytes(b"test executable contract")
        self.output = self.root / "builds"
        self.temp = self.root / "temp"
        self.config = {
            "workspace_root": str(self.workspace), "test_workspace": str(self.root),
            "runs_root": str(self.root / "runs"), "reports_root": str(self.root / "reports"),
            "builds_root": str(self.output), "temp_root": str(self.temp),
            "tools_root": str(self.tools), "workdrive": str(self.root),
            "deployment_targets": [str(self.root / "installed")],
        }
        self.original_runtime_config = pipeline.runtime_config
        self.configuration_patch = patch.object(pipeline, "runtime_config", return_value=self.config)
        self.configuration_patch.start()
        self.addCleanup(self.configuration_patch.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def source(self):
        pipeline.scaffold(self.workspace, "TestMod", "server")
        return self.workspace / "TestMod"

    def fake_builder(self, command, **arguments):
        stage = Path(command[1])
        output = Path(command[2])
        self.assertEqual(Path(arguments["env"]["TEMP"]).drive, self.root.drive)
        self.assertEqual(arguments["env"]["TEMP"], arguments["env"]["TMP"])
        self.assertIn("-packonly", command)
        self.assertFalse(any(item.startswith("-sign") for item in command))
        entries = [(file.relative_to(stage).as_posix(), file.read_bytes(), 0)
                   for file in pipeline.package_files(stage) if file.name != "$PBOPREFIX$"]
        write_pbo(output / (stage.name + ".pbo"), entries, prefix=pipeline.prefix_for(stage))
        return subprocess.CompletedProcess(command, 0, "Build succeeded", "")

    def test_scaffold_is_lintable_and_does_not_overwrite(self):
        source = self.source()
        self.assertEqual(pipeline.lint(source)["status"], "passed")
        self.assertIn("-serverMod", (source / "README.md").read_text(encoding="utf-8"))
        with self.assertRaises(pipeline.PipelineError):
            pipeline.scaffold(self.workspace, "TestMod")
        self.assertEqual(pipeline.prefix_for(source), "TestMod")

    def test_scaffold_rejects_path_name(self):
        for name in ("../Other", "A:B", "A/B", "bad-name", "x", "A\nB"):
            with self.subTest(name=name), self.assertRaises(pipeline.PipelineError):
                pipeline.scaffold(self.workspace, name)

    def test_lint_reports_missing_script_directory(self):
        source = self.source()
        (source / "Scripts/4_World").rmdir()
        result = pipeline.lint(source)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("4_World" in entry["message"] for entry in result["findings"]))

    def test_lint_ignores_comments_and_braces_in_strings(self):
        source = self.source()
        path = source / "config.cpp"
        path.write_text(path.read_text(encoding="utf-8") + '\n// class Fake {\nclass BraceString { value = "}"; };\n', encoding="utf-8")
        self.assertEqual(pipeline.lint(source)["status"], "passed")

    def test_lint_checks_own_resource_without_changing_ogg_contract(self):
        source = self.source()
        path = source / "config.cpp"
        path.write_text(path.read_text(encoding="utf-8") + '\nclass CfgSoundShaders { class Voice { samples[] = {{"TestMod/Sounds/voice.ogg", 1}}; }; };\n', encoding="utf-8")
        self.assertEqual(pipeline.lint(source)["status"], "failed")
        (source / "Sounds").mkdir()
        (source / "Sounds/voice.ogg").write_bytes(b"OggS")
        self.assertEqual(pipeline.lint(source)["status"], "passed")

    def test_own_prefix_case_difference_does_not_hide_missing_resource(self):
        source = self.source()
        path = source / "config.cpp"
        path.write_text(path.read_text(encoding="utf-8") + '\nclass S { samples[] = {{"testmod/Sounds/absent.ogg", 1}}; };\n', encoding="utf-8")
        value = pipeline.lint(source)
        self.assertEqual(value["status"], "failed")
        self.assertTrue(any("absent.ogg" in finding["message"] for finding in value["findings"]))

    def test_invalid_virtual_paths_fail_instead_of_resolving_outside_source(self):
        source = self.source()
        path = source / "config.cpp"
        path.write_text(path.read_text(encoding="utf-8") + '\nclass S { samples[] = {{"TestMod//outside.ogg", 1}}; };\n', encoding="utf-8")
        self.assertEqual(pipeline.lint(source)["status"], "failed")

    def test_private_material_is_blocked_before_packaging(self):
        source = self.source()
        (source / "bad.biprivatekey").write_bytes(b"private")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.package_files(source)

    def test_text_secret_is_not_returned_in_error(self):
        source = self.source()
        secret = "NotARealSecret12345"
        (source / "settings.json").write_text(json.dumps({"password": secret}), encoding="utf-8")
        with self.assertRaises(pipeline.PipelineError) as caught:
            pipeline.package_files(source)
        self.assertNotIn(secret, str(caught.exception))

    def test_benign_secret_word_is_allowed(self):
        source = self.source()
        (source / "Scripts/3_Game/SecretDoor.c").write_text('class SecretDoor { string Label = "secret cave"; }\n', encoding="utf-8")
        self.assertEqual(pipeline.lint(source)["status"], "passed")

    def test_config_bin_is_partial_not_passed(self):
        source = self.source()
        (source / "config.cpp").unlink()
        (source / "config.bin").write_bytes(b"rap\0")
        self.assertEqual(pipeline.lint(source)["status"], "partial")

    def test_symlink_is_rejected(self):
        source = self.source()
        outside = self.root / "outside.c"
        outside.write_text("class Outside {}", encoding="utf-8")
        link = source / "linked.c"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest("OS не разрешает symlink: " + str(error))
        with self.assertRaises(pipeline.PipelineError):
            pipeline.package_files(source)

    def test_inspect_hashes_real_payload_and_trailer(self):
        path = write_pbo(self.root / "input/TestMod.pbo", [("Scripts/3_Game/Test.c", b"class T {}", 0)])
        result = pipeline.inspect_pbo(path)
        self.assertEqual(result["checksum"], "verified")
        self.assertEqual(result["prefix"], "TestMod")
        self.assertEqual(result["entries"][0]["content_sha256"], hashlib.sha256(b"class T {}").hexdigest())

    def test_trailing_prefix_separator_is_normalized(self):
        path = write_pbo(self.root / "input/TestMod.pbo", [("file.c", b"code", 0)], prefix="TestMod\\")
        value = pipeline.inspect_pbo(path)
        self.assertEqual(value["prefix"], "TestMod")
        self.assertEqual(value["properties"]["prefix"], "TestMod\\")
        source = self.source()
        (source / "$PBOPREFIX$").write_text("TestMod\\\n", encoding="utf-8")
        self.assertEqual(pipeline.prefix_for(source), "TestMod")

    def test_inspect_rejects_file_changed_during_hash(self):
        path = write_pbo(self.root / "input/TestMod.pbo", [("file.c", b"code", 0)])
        original = pipeline.sha256
        def changed(value):
            digest = original(value)
            with path.open("ab") as stream:
                stream.write(b"changed")
            return digest
        with patch.object(pipeline, "sha256", side_effect=changed):
            with self.assertRaisesRegex(pipeline.PipelineError, "изменился"):
                pipeline.inspect_pbo(path)

    def test_compressed_content_is_explicitly_unverified(self):
        path = write_pbo(self.root / "input/TestMod.pbo", [("file.c", b"compressed", pipeline.CPRS)])
        self.assertIsNone(pipeline.inspect_pbo(path)["entries"][0]["content_sha256"])

    def test_malicious_pbo_paths_and_duplicates_are_rejected(self):
        for name in ("../escape.c", "A/../../escape.c", "C:/escape.c", "/absolute.c", "a/./file.c", "a//file.c", "key.biprivatekey"):
            with self.subTest(name=name):
                path = write_pbo(self.root / "input/TestMod.pbo", [(name, b"x", 0)])
                with self.assertRaises(pipeline.PipelineError):
                    pipeline.inspect_pbo(path)
        path = write_pbo(self.root / "input/TestMod.pbo", [("File.c", b"x", 0), ("file.c", b"y", 0)])
        with self.assertRaises(pipeline.PipelineError):
            pipeline.inspect_pbo(path)

    def test_truncated_header_payload_and_bad_checksum_rejected(self):
        path = self.root / "input/TestMod.pbo"
        write_pbo(path, [("file.c", b"payload", 0)])
        full = path.read_bytes()
        for data in (full[:3], full[:30], full[:-25], full[:-1], full[:-1] + b"X"):
            with self.subTest(length=len(data)):
                path.write_bytes(data)
                with self.assertRaises(pipeline.PipelineError):
                    pipeline.inspect_pbo(path)

    def test_preflight_does_not_launch_tools(self):
        with patch.object(pipeline.subprocess, "run") as run:
            result = pipeline.preflight(self.tools)
        run.assert_not_called()
        self.assertTrue(result["tools"]["AddonBuilder"]["available"])
        self.assertFalse(result["tools"]["CfgConvert"]["available"])

    def test_preflight_uses_configured_workdrive_not_a_fixed_drive(self):
        value = pipeline.preflight()
        self.assertEqual(value["workdrive"], str(self.root))
        self.assertTrue(value["workdrive_present"])
        self.assertTrue(value["builtin_pack_available"])

    def test_build_plan_is_nonmutating(self):
        source = self.source()
        with patch.object(pipeline.subprocess, "run") as run:
            result = pipeline.build(source, self.output, self.tools, self.temp)
        self.assertEqual(result["status"], "planned")
        self.assertFalse(self.output.exists())
        self.assertFalse(self.temp.exists())
        run.assert_not_called()

    def test_build_uses_new_stage_and_reads_back_every_plain_entry(self):
        source = self.source()
        self.output.mkdir()
        old = self.output / "TestMod.pbo"
        old.write_bytes(b"old invalid build")
        with patch.object(pipeline.subprocess, "run", side_effect=self.fake_builder):
            result = pipeline.build(source, self.output, self.tools, self.temp, run=True, backend="addonbuilder")
        self.assertEqual(result["status"], "passed")
        self.assertIn("config.cpp", result["content_verified"])
        self.assertIn("Scripts/3_Game/TestMod_BuildInfo.c", result["content_verified"])
        self.assertEqual(pipeline.sha256(old), result["sha256"])
        self.assertTrue((self.output / "TestMod.pbo.receipt.json").exists())

    def test_failed_builder_cannot_promote_existing_output(self):
        source = self.source()
        self.output.mkdir()
        old = self.output / "TestMod.pbo"
        old.write_bytes(b"previous")
        with patch.object(pipeline.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            result = pipeline.build(source, self.output, self.tools, self.temp, run=True, backend="addonbuilder")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(old.read_bytes(), b"previous")

    def test_pbo_readback_mismatch_does_not_replace_output(self):
        source = self.source()
        def wrong_builder(command, **kwargs):
            self.fake_builder(command, **kwargs)
            stage_output = Path(command[2]) / "TestMod.pbo"
            write_pbo(stage_output, [("config.cpp", b"wrong", 0)], prefix="TestMod")
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(pipeline.subprocess, "run", side_effect=wrong_builder):
            with self.assertRaises(pipeline.PipelineError):
                pipeline.build(source, self.output, self.tools, self.temp, run=True, backend="addonbuilder")
        self.assertFalse((self.output / "TestMod.pbo").exists())

    def test_source_drift_prevents_promotion(self):
        source = self.source()
        def changing_builder(command, **kwargs):
            result = self.fake_builder(command, **kwargs)
            (source / "config.cpp").write_text("changed", encoding="utf-8")
            return result
        with patch.object(pipeline.subprocess, "run", side_effect=changing_builder):
            with self.assertRaises(pipeline.PipelineError):
                pipeline.build(source, self.output, self.tools, self.temp, run=True, backend="addonbuilder")
        self.assertFalse((self.output / "TestMod.pbo").exists())

    def test_new_source_file_during_build_prevents_promotion(self):
        source = self.source()
        def adding_builder(command, **kwargs):
            result = self.fake_builder(command, **kwargs)
            (source / "Scripts/3_Game/Added.c").write_text("class Added {}\n", encoding="utf-8")
            return result
        with patch.object(pipeline.subprocess, "run", side_effect=adding_builder):
            with self.assertRaisesRegex(pipeline.PipelineError, "Состав исходников"):
                pipeline.build(source, self.output, self.tools, self.temp, run=True, backend="addonbuilder")
        self.assertFalse((self.output / "TestMod.pbo").exists())

    def test_failed_native_exit_never_promotes_even_valid_artifact(self):
        source = self.source()
        def error_builder(command, **kwargs):
            self.fake_builder(command, **kwargs)
            return subprocess.CompletedProcess(command, 1, "configuration conversion failed", "")
        with patch.object(pipeline.subprocess, "run", side_effect=error_builder):
            value = pipeline.build(source, self.output, self.tools, self.temp, run=True, backend="addonbuilder")
        self.assertEqual(value["status"], "failed")
        self.assertFalse((self.output / "TestMod.pbo").exists())

    def test_build_rejects_overlapping_roots(self):
        source = self.source()
        with self.assertRaises(pipeline.PipelineError):
            pipeline.build(source, source / "output", self.tools, self.temp)
        with self.assertRaises(pipeline.PipelineError):
            pipeline.build(source, self.output, self.tools, self.output / "temp")

    def test_builtin_build_is_real_and_does_not_launch_a_process(self):
        source = self.source()
        (source / "Sounds").mkdir()
        audio = b"OggS\0\x80\xff\0payload"
        (source / "Sounds/voice.ogg").write_bytes(audio)
        with patch.object(pipeline.subprocess, "run") as process:
            result = pipeline.build(source, self.output, self.root / "tools_not_installed", self.temp, run=True)
        process.assert_not_called()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["backend"], "builtin")
        self.assertIn("Binarize", result["skipped"])
        self.assertEqual(result["compilation"]["Enforce"], "not_run")
        inspected = pipeline.inspect_pbo(self.output / "TestMod.pbo")
        self.assertEqual(inspected["checksum"], "verified")
        entries = {entry["name"]: entry for entry in inspected["entries"]}
        self.assertEqual(entries["Sounds/voice.ogg"]["content_sha256"], hashlib.sha256(audio).hexdigest())
        self.assertEqual(entries["config.cpp"]["content_sha256"], pipeline.sha256(source / "config.cpp"))

    def test_builtin_output_is_deterministic_across_mtime_and_session(self):
        source = self.source()
        first = pipeline.build(source, self.output, temp=self.temp, run=True)
        for path in pipeline.package_files(source):
            os.utime(path, (1000000000, 1000000000))
        second = pipeline.build(source, self.root / "other-output", temp=self.temp, run=True)
        self.assertNotEqual(first["session"], second["session"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual((self.output / "TestMod.pbo").read_bytes(), (self.root / "other-output/TestMod.pbo").read_bytes())

    def test_native_timeout_reports_that_execution_started(self):
        source = self.source()
        with patch.object(pipeline.subprocess, "run", side_effect=subprocess.TimeoutExpired("AddonBuilder", 1)):
            value = pipeline.build(source, self.output, self.tools, self.temp, run=True, backend="addonbuilder", timeout=1)
        self.assertEqual(value["status"], "failed")
        self.assertIn("AddonBuilder запущен", value["coverage"])
        self.assertFalse(any("не запуск" in item for item in value["skipped"]))

    def test_writes_require_configured_roots_on_every_platform(self):
        with patch.object(pipeline, "runtime_config", return_value={"builds_root": str(self.output)}):
            self.assertEqual(pipeline.checked_path(self.output / "a.pbo", write=True), self.output / "a.pbo")
            with self.assertRaisesRegex(pipeline.PipelineError, "не зарегистрирован"):
                pipeline.checked_path(self.workspace / "other.pbo", write=True)

    def test_relative_config_roots_are_based_on_agent_directory(self):
        with patch.object(pipeline, "AGENT_ROOT", self.root):
            with patch.object(pipeline, "runtime_config", return_value={"builds_root": "builds"}):
                self.assertEqual(pipeline.checked_path(self.output / "a.pbo", write=True), self.output / "a.pbo")

    def test_extra_approved_root_and_config_file_override(self):
        configuration = self.root / "local-config.json"
        configuration.write_text(json.dumps({"approved_write_roots": [str(self.output)]}), encoding="utf-8")
        with patch.dict(os.environ, {"BOBR_WORKBENCH_CONFIG": str(configuration)}):
            self.assertEqual(self.original_runtime_config()["approved_write_roots"], [str(self.output)])
        with patch.object(pipeline, "runtime_config", return_value={"approved_write_roots": [str(self.output)]}):
            self.assertEqual(pipeline.checked_path(self.output, write=True), self.output)

    def test_build_uses_configured_temp_and_needs_no_native_tools(self):
        source = self.source()
        self.config.pop("tools_root")
        value = pipeline.build(source, self.output, run=True)
        self.assertEqual(value["backend"], "builtin")
        self.assertTrue(Path(value["session"]).is_relative_to(self.temp))

    def test_build_blocks_configured_game_but_not_a_directory_named_steamapps(self):
        source = self.source()
        self.config["game_root"] = str(self.root / "game")
        with self.assertRaisesRegex(pipeline.PipelineError, "установленную игру"):
            pipeline.build(source, self.root / "game/addons", temp=self.temp)
        value = pipeline.build(source, self.root / "steamapps/dev-builds", temp=self.temp)
        self.assertEqual(value["status"], "planned")

    def test_deploy_plan_and_exact_replace(self):
        source = write_pbo(self.root / "input/TestMod.pbo", [("file.c", b"code", 0)])
        target = self.root / "installed"
        self.assertEqual(pipeline.deploy(source, target)["status"], "planned")
        self.assertFalse(target.exists())
        result = pipeline.deploy(source, target, run=True)
        self.assertEqual(result["status"], "passed")
        self.assertEqual((target / source.name).read_bytes(), source.read_bytes())
        unrelated = target / "OtherMod.pbo"
        unrelated.write_bytes(b"do not touch")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.deploy(source, target, run=True)
        pipeline.deploy(source, target, run=True, replace=True)
        self.assertEqual(unrelated.read_bytes(), b"do not touch")
        self.assertFalse(list(target.glob("*.tmp")))

    def test_stale_signatures_block_deployment(self):
        source = write_pbo(self.root / "input/TestMod.pbo", [("file.c", b"code", 0)])
        target = self.root / "installed"
        target.mkdir()
        (target / "TestMod.pbo.Author.bisign").write_bytes(b"old signature")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.deploy(source, target, run=True, replace=True)
        self.assertFalse((target / source.name).exists())

    def test_unregistered_deployment_can_be_planned_but_not_executed(self):
        source = write_pbo(self.root / "input/TestMod.pbo", [("file.c", b"code", 0)])
        target = self.root / "unregistered"
        value = pipeline.deploy(source, target)
        self.assertTrue(value["execution_blocked"])
        self.assertFalse(value["target_registered"])
        with self.assertRaisesRegex(pipeline.PipelineError, "deployment_targets"):
            pipeline.deploy(source, target, run=True)
        self.assertFalse(target.exists())

    def test_deployment_registration_is_exact_not_an_entire_drive(self):
        source = write_pbo(self.root / "input/TestMod.pbo", [("file.c", b"code", 0)])
        with self.assertRaisesRegex(pipeline.PipelineError, "deployment_targets"):
            pipeline.deploy(source, self.root / "installed/other", run=True)

    def test_cli_failure_is_nonzero_machine_readable(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = pipeline.main(["inspect-pbo", "--pbo", str(self.root / "absent.pbo")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stream.getvalue())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
