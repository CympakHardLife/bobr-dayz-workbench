"""Контракт заявленных результатов: drift, неполнота, сторона и локальные evidence."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cli
import scenarios as s


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("BOBR_TEST_ROOT", tempfile.gettempdir()))
        self.root = Path(self.temp.name)
        (self.root / "source").mkdir()
        (self.root / "source/test.c").write_text("class OwnFixture {};", encoding="utf-8")
        self.pbo = self.root / "own.pbo"
        self.pbo.write_bytes(b"artifact identity fixture; no PBO validity claim")
        self.evidence = self.root / "result.log"
        self.evidence.write_text("observed fixture result", encoding="utf-8")
        self.build = {"id": "fixture-1", "source": {"root": "source", "sha256": s.source_hash(self.root / "source")},
                      "pbos": [{"path": "own.pbo", "sha256": s.file_hash(self.pbo)}]}
        self.spec = {"schema_version": 1, "build": copy.deepcopy(self.build),
                     "scenarios": [{"id": "own-smoke", "side": "server", "expected": "contact_preserved"}]}
        self.report = {"schema_version": 1, "build": copy.deepcopy(self.build), "scenarios": [
            {"id": "own-smoke", "side": "server", "events": [{"event": "START"},
             {"event": "PASS", "actual": "contact_preserved", "evidence": [{"path": "result.log", "sha256": s.file_hash(self.evidence)}]}]}]}

    def tearDown(self):
        self.temp.cleanup()

    def paths(self):
        spec_path, results_path = self.root / "spec.json", self.root / "results.json"
        spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        results_path.write_text(json.dumps(self.report), encoding="utf-8")
        return spec_path, results_path

    def validate(self):
        return s.validate(*self.paths())

    def test_completed_pass_has_checked_hash_evidence_and_honest_limit(self):
        value = self.validate()
        self.assertEqual(value["status"], "pass")
        self.assertEqual(value["scenarios"][0]["evidence"][0]["sha256"], s.file_hash(self.evidence))
        self.assertIn("truth_of_observer_claim", value["skipped"])

    def test_absent_and_empty_unstarted_are_not_run(self):
        for rows in ([], [{"id": "own-smoke", "side": "server", "events": []}]):
            self.report["scenarios"] = rows
            self.assertEqual(self.validate()["status"], "not_run")

    def test_started_without_terminal_fails(self):
        self.report["scenarios"][0]["events"].pop()
        value = self.validate()
        self.assertEqual(value["status"], "fail")
        self.assertEqual(value["scenarios"][0]["reason"], "started_without_terminal")

    def test_reported_failure_and_expected_mismatch_fail(self):
        terminal = self.report["scenarios"][0]["events"][1]
        terminal["event"] = "FAIL"
        self.assertEqual(self.validate()["status"], "fail")
        terminal.update(event="PASS", actual="contact_lost")
        self.assertEqual(self.validate()["status"], "fail")

    def test_partial_pass_does_not_claim_overall_pass(self):
        self.spec["scenarios"].append({"id": "enemy-smoke", "side": "client_server", "expected": "clue_created"})
        self.assertEqual(self.validate()["status"], "not_run")

    def test_source_file_change_add_remove_and_pbo_drift_rejected(self):
        target = self.root / "source/test.c"
        original = target.read_bytes()
        target.write_bytes(b"class Changed {};")
        with self.assertRaises(ValueError): self.validate()
        target.write_bytes(original)
        extra = self.root / "source/new.c"
        extra.write_text("class Added {};", encoding="utf-8")
        with self.assertRaises(ValueError): self.validate()
        extra.unlink()
        target.unlink()
        with self.assertRaises(ValueError): self.validate()
        target.write_bytes(original)
        self.pbo.write_bytes(b"different artifact")
        with self.assertRaises(ValueError): self.validate()

    def test_foreign_build_and_side_rejected(self):
        self.report["build"]["id"] = "another"
        with self.assertRaises(ValueError): self.validate()
        self.report["build"] = copy.deepcopy(self.build)
        self.report["scenarios"][0]["side"] = "client"
        with self.assertRaises(ValueError): self.validate()

    def test_missing_changed_and_empty_evidence_rejected(self):
        terminal = self.report["scenarios"][0]["events"][1]
        self.evidence.write_text("changed evidence", encoding="utf-8")
        with self.assertRaises(ValueError): self.validate()
        self.evidence.unlink()
        with self.assertRaises(ValueError): self.validate()
        terminal["evidence"] = []
        with self.assertRaises(ValueError): self.validate()

    def test_duplicate_and_foreign_ids_rejected(self):
        self.report["scenarios"].append(copy.deepcopy(self.report["scenarios"][0]))
        with self.assertRaises(ValueError): self.validate()
        self.report["scenarios"].pop()
        self.report["scenarios"][0]["id"] = "foreign"
        with self.assertRaises(ValueError): self.validate()

    def test_duplicate_expected_id_and_duplicate_start_rejected(self):
        self.spec["scenarios"].append(copy.deepcopy(self.spec["scenarios"][0]))
        with self.assertRaises(ValueError): self.validate()
        self.spec["scenarios"].pop()
        self.report["scenarios"][0]["events"][1] = {"event": "START"}
        with self.assertRaises(ValueError): self.validate()

    def test_terminal_without_start_rejected(self):
        self.report["scenarios"][0]["events"].pop(0)
        with self.assertRaises(ValueError): self.validate()

    def test_duplicate_json_keys_rejected(self):
        spec_path, report_path = self.paths()
        report_path.write_text('{"build":{},"build":{},"schema_version":1,"scenarios":[]}', encoding="utf-8")
        with self.assertRaises(ValueError): s.validate(spec_path, report_path)

    def test_atomic_replacement_with_same_size_and_mtime_is_rejected(self):
        original_open = Path.open
        before = self.evidence.stat()
        replacement = self.root / "replacement.log"
        replacement.write_bytes(b"x" * before.st_size)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        @contextlib.contextmanager
        def replaced(path, *args, **kwargs):
            with original_open(path, *args, **kwargs) as stream:
                yield stream
            if path == self.evidence:
                os.replace(replacement, path)
        with patch.object(Path, "open", replaced):
            with self.assertRaisesRegex(ValueError, "Файл изменился"):
                s.file_hash(self.evidence)

    def test_evidence_changed_during_final_build_check_is_rejected(self):
        original = s.verify_build
        calls = 0
        def changed(build, base):
            nonlocal calls
            value = original(build, base)
            calls += 1
            if calls == 2:
                self.evidence.write_text("changed during final source rehash", encoding="utf-8")
            return value
        with patch.object(s, "verify_build", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "SHA drift evidence"):
                self.validate()

    def test_private_path_rejected_before_hash_read(self):
        self.report["scenarios"][0]["events"][1]["evidence"][0]["path"] = "keys/fixture.log"
        with self.assertRaises(ValueError): self.validate()

    def test_cli_codes_and_no_history_or_child_process(self):
        spec_path, report_path = self.paths()
        with patch.object(cli.subprocess, "run", side_effect=AssertionError("no process")), patch.object(cli, "atomic_json", side_effect=AssertionError("no history write")), contextlib.redirect_stdout(io.StringIO()) as stream:
            code = cli.main(["scenario-results", "--spec", str(spec_path), "--results", str(report_path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stream.getvalue())["status"], "pass")
        self.report["scenarios"] = []
        self.paths()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(s.main(["--spec", str(spec_path), "--results", str(report_path)]), 1)
        self.report["build"]["id"] = "foreign-build"
        self.paths()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(s.main(["--spec", str(spec_path), "--results", str(report_path)]), 2)


if __name__ == "__main__":
    unittest.main()
