"""Проверки контракта оркестрации на изолированном проекте E:."""

import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest

from orchestration import TaskError, TaskStore, main, path_overlaps, read_json


TEST_ROOT = Path(os.environ.get("BOBR_TEST_ROOT", tempfile.gettempdir())) / "Workbench_Orchestration"


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=TEST_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = TaskStore(self.root / "runs")
        self.plan = {"title": "Проверка полного цикла", "tasks": [
            {"id": "code", "title": "Код", "role": "script-specialist", "skills": [],
             "depends_on": [], "write_paths": ["scripts"], "acceptance": ["Проверка пройдена"]},
            {"id": "package", "title": "Упаковка", "role": "build-engineer", "skills": [],
             "depends_on": ["code"], "write_paths": ["dist"], "acceptance": ["Архив читается"]},
        ]}
        (self.project / "evidence.txt").write_text("revision-one", encoding="utf-8")
        (self.project / "package.txt").write_text("package-one", encoding="utf-8")

    def result(self, evidence="evidence.txt", status="pass"):
        return {"summary": "Статический результат", "evidence": [evidence],
                "checks": [{"name": "Проверка", "status": status, "details": "Реальный отчёт"}],
                "limitations": ["Игра не запускалась"]}

    def create(self, plan=None):
        return self.store.create(plan or self.plan, self.project)["id"]

    def accept(self, run, task="code", evidence="evidence.txt"):
        self.store.claim(run, task, "worker")
        self.store.submit(run, task, "worker", self.result(evidence))
        self.store.review(run, task, "reviewer", "accept", "Проверил файлы и отчёт")

    def test_full_cycle_and_resume(self):
        run = self.create()
        self.assertEqual([t["id"] for t in self.store.next(run)["ready"]], ["code"])
        self.accept(run)
        resumed = TaskStore(self.root / "runs")
        self.assertEqual([t["id"] for t in resumed.next(run)["ready"]], ["package"])
        self.accept(run, "package", "package.txt")
        self.assertTrue(resumed.next(run)["complete"])
        self.assertEqual(resumed.show(run)["tasks"]["code"]["status"], "done")

    def test_cycle_and_unknown_dependency(self):
        plan = copy.deepcopy(self.plan)
        plan["tasks"][0]["depends_on"] = ["package"]
        with self.assertRaisesRegex(TaskError, "Цикл"):
            self.create(plan)
        plan["tasks"][0]["depends_on"] = ["unknown"]
        with self.assertRaisesRegex(TaskError, "Неизвестная"):
            self.create(plan)

    def test_parallel_path_collision_and_case(self):
        plan = copy.deepcopy(self.plan)
        plan["tasks"][1]["depends_on"] = []
        plan["tasks"][1]["write_paths"] = ["Scripts/item.c"]
        with self.assertRaisesRegex(TaskError, "Параллельные"):
            self.create(plan)
        self.assertTrue(path_overlaps("a/b", "A/b/file"))
        self.assertFalse(path_overlaps("a/b", "a/bb"))

    def test_shared_paths_allowed_for_ordered_tasks(self):
        plan = copy.deepcopy(self.plan)
        plan["tasks"][1]["write_paths"] = ["scripts/next.c"]
        self.create(plan)

    def test_unaccepted_dependency_blocks_claim(self):
        run = self.create()
        with self.assertRaisesRegex(TaskError, "Зависимости"):
            self.store.claim(run, "package", "worker")

    def test_actor_and_reviewer_are_separate(self):
        run = self.create()
        self.store.claim(run, "code", "worker")
        with self.assertRaisesRegex(TaskError, "текущий исполнитель"):
            self.store.submit(run, "code", "other", self.result())
        self.store.submit(run, "code", "worker", self.result())
        with self.assertRaisesRegex(TaskError, "независимый"):
            self.store.review(run, "code", "worker", "accept", "Сам принял")
        self.assertEqual(self.store.show(run)["tasks"]["code"]["status"], "review")

    def test_failed_checks_cannot_be_accepted(self):
        run = self.create()
        self.store.claim(run, "code", "worker")
        self.store.submit(run, "code", "worker", self.result(status="fail"))
        with self.assertRaisesRegex(TaskError, "упавшими"):
            self.store.review(run, "code", "reviewer", "accept", "Пытался принять")
        self.store.review(run, "code", "reviewer", "reject", "Нужно исправить")
        self.assertEqual(self.store.next(run)["ready"][0]["id"], "code")
        self.store.claim(run, "code", "worker-again")
        self.store.submit(run, "code", "worker-again", self.result())
        history = self.store.show(run)["tasks"]["code"]["submissions"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["result"]["checks"][0]["status"], "fail")

    def test_reopen_recovers_interrupted_worker_and_invalidates_descendants(self):
        run = self.create()
        self.store.claim(run, "code", "disconnected-worker")
        self.store.reopen(run, "code", "primary-agent", "Прошлый процесс завершился; продолжаем")
        self.assertEqual(self.store.next(run)["ready"][0]["id"], "code")
        self.accept(run)
        self.accept(run, "package", "package.txt")
        self.store.reopen(run, "code", "primary-agent", "Найден новый дефект")
        state = self.store.show(run)
        self.assertEqual(state["tasks"]["code"]["status"], "pending")
        self.assertEqual(state["tasks"]["package"]["status"], "stale")
        self.assertEqual(len(state["tasks"]["code"]["submissions"]), 1)

    def test_not_run_requires_limitations(self):
        run = self.create()
        self.store.claim(run, "code", "worker")
        result = self.result(status="not_run")
        result["limitations"] = []
        with self.assertRaisesRegex(TaskError, "ограничений"):
            self.store.submit(run, "code", "worker", result)
        result["limitations"] = ["Игра не запускалась; результат только файлы"]
        self.store.submit(run, "code", "worker", result)
        accepted = self.store.review(run, "code", "reviewer", "accept", "Только поставленные файлы")
        self.assertEqual(accepted["result"]["scope"], "submitted_files_and_named_checks_only")

    def test_missing_evidence_and_empty_result_rejected(self):
        run = self.create()
        self.store.claim(run, "code", "worker")
        for result in ({}, self.result("missing.txt"), {**self.result(), "evidence": []}):
            with self.assertRaises(TaskError):
                self.store.submit(run, "code", "worker", result)

    def test_stale_done_invalidates_all_descendants(self):
        run = self.create()
        self.accept(run)
        self.accept(run, "package", "package.txt")
        (self.project / "evidence.txt").write_text("changed", encoding="utf-8")
        state = self.store.show(run)
        self.assertEqual(state["tasks"]["code"]["status"], "stale")
        self.assertEqual(state["tasks"]["package"]["status"], "stale")
        self.assertFalse(self.store.next(run)["complete"])
        self.assertEqual([t["id"] for t in self.store.next(run)["ready"]], ["code"])

    def test_stale_review_cannot_be_accepted(self):
        run = self.create()
        self.store.claim(run, "code", "worker")
        self.store.submit(run, "code", "worker", self.result())
        (self.project / "evidence.txt").unlink()
        with self.assertRaisesRegex(TaskError, "актуальный"):
            self.store.review(run, "code", "reviewer", "accept", "Файла уже нет")
        self.assertEqual(self.store.show(run)["tasks"]["code"]["status"], "stale")

    def test_only_one_concurrent_claim_succeeds(self):
        run = self.create()
        def claim(index):
            try:
                TaskStore(self.root / "runs").claim(run, "code", "worker-" + str(index))
                return True
            except TaskError:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(claim, range(6)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.show(run)["tasks"]["code"]["status"], "running")

    def test_other_run_cannot_claim_same_project_paths(self):
        first, second = self.create(), self.create()
        self.store.claim(first, "code", "author-first")
        with self.assertRaisesRegex(TaskError, "Файлы заняты"):
            self.store.claim(second, "code", "author-second")
        self.store.submit(first, "code", "author-first", self.result())
        with self.assertRaisesRegex(TaskError, "Файлы заняты"):
            self.store.claim(second, "code", "author-second")
        self.store.review(first, "code", "reviewer", "accept", "Результат проверен")
        self.store.claim(second, "code", "author-second")

    def test_cross_run_ownership_uses_absolute_paths_for_nested_projects(self):
        scripts = self.project / "scripts"
        scripts.mkdir()
        first = self.create()
        other_plan = {"title": "Вложенный проект", "tasks": [
            {"id": "edit", "title": "Правка", "role": "dayz-script", "skills": [],
             "depends_on": [], "write_paths": ["item.c"], "acceptance": ["Проверено"]}
        ]}
        second = self.store.create(other_plan, scripts)["id"]
        self.store.claim(first, "code", "author-first")
        with self.assertRaisesRegex(TaskError, "Файлы заняты"):
            self.store.claim(second, "edit", "author-second")

    def test_unrelated_projects_can_use_same_relative_paths(self):
        other_project = self.root / "another-project"
        other_project.mkdir()
        first = self.create()
        second = self.store.create(self.plan, other_project)["id"]
        self.store.claim(first, "code", "author-first")
        self.store.claim(second, "code", "author-second")
        self.assertEqual(self.store.show(second)["tasks"]["code"]["status"], "running")

    def test_only_one_concurrent_cross_run_claim_succeeds(self):
        runs = [self.create(), self.create()]
        def claim(index):
            try:
                TaskStore(self.root / "runs").claim(runs[index], "code", "author-" + str(index))
                return True
            except TaskError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, range(2)))
        self.assertEqual(sum(outcomes), 1)

    def test_prior_author_cannot_review_after_reassignment(self):
        run = self.create()
        self.store.claim(run, "code", "original-author")
        self.store.submit(run, "code", "original-author", self.result())
        self.store.review(run, "code", "independent", "reject", "Нужно исправить")
        self.store.claim(run, "code", "replacement-author")
        self.store.submit(run, "code", "replacement-author", self.result())
        with self.assertRaisesRegex(TaskError, "независимый"):
            self.store.review(run, "code", "original-author", "accept", "Пытаюсь принять своё")
        self.store.review(run, "code", "independent", "accept", "Проверено независимо")

    def test_contributor_provenance_survives_submission_history_limit(self):
        run = self.create()
        for index in range(18):
            actor = "author-" + str(index)
            self.store.claim(run, "code", actor)
            self.store.submit(run, "code", actor, self.result())
            if index < 17:
                self.store.review(run, "code", "independent", "reject", "Следующая исправленная попытка")
        task = self.store.show(run)["tasks"]["code"]
        self.assertEqual(len(task["submissions"]), 16)
        self.assertIn("author-0", task["contributors"])
        with self.assertRaisesRegex(TaskError, "независимый"):
            self.store.review(run, "code", "author-0", "accept", "Ранний автор не независим")

    def test_legacy_run_recovers_contributors_before_claim(self):
        run = self.create()
        self.store.claim(run, "code", "original-author")
        self.store.submit(run, "code", "original-author", self.result())
        self.store.review(run, "code", "independent", "reject", "Нужно исправить")
        path = self.root / "runs" / (run + ".json")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["tasks"]["code"].pop("contributors")
        path.write_text(json.dumps(value), encoding="utf-8")
        resumed = TaskStore(self.root / "runs")
        resumed.claim(run, "code", "new-author")
        resumed.submit(run, "code", "new-author", self.result())
        with self.assertRaisesRegex(TaskError, "независимый"):
            resumed.review(run, "code", "original-author", "accept", "История не должна забываться")

    def test_corrupted_state_is_preserved(self):
        run = self.create()
        path = self.root / "runs" / (run + ".json")
        path.write_text("broken{", encoding="utf-8")
        with self.assertRaisesRegex(TaskError, "Повреждённый JSON"):
            self.store.show(run)
        self.assertEqual(path.read_text(encoding="utf-8"), "broken{")

    def test_path_escape_and_private_paths_rejected(self):
        for path in ("../escape", "/absolute", "C:/elsewhere", "scripts/../evil", "Secrets/token.txt", ".env", "key.biprivatekey"):
            plan = copy.deepcopy(self.plan)
            plan["tasks"][0]["write_paths"] = [path]
            with self.assertRaises(TaskError, msg=path):
                self.create(plan)

    def test_private_json_input_is_rejected_without_read(self):
        private = self.root / "Secrets"
        private.mkdir()
        target = private / "data.json"
        target.write_text('{"secret": "not-for-reading"}', encoding="utf-8")
        with self.assertRaisesRegex(TaskError, "Приватный"):
            read_json(target)

    def test_symlink_evidence_rejected_when_supported(self):
        link = self.project / "linked.txt"
        try:
            link.symlink_to(self.project / "evidence.txt")
        except OSError as exc:
            self.skipTest("ОС не разрешила тестовую symlink: " + str(exc))
        run = self.create()
        self.store.claim(run, "code", "worker")
        with self.assertRaisesRegex(TaskError, "Symlink"):
            self.store.submit(run, "code", "worker", self.result("linked.txt"))

    def test_brief_is_bounded_and_marks_missing_profiles(self):
        plan = copy.deepcopy(self.plan)
        plan["tasks"][0]["role"] = "not-installed-fixture-role"
        plan["tasks"][0]["skills"] = ["not-installed-fixture-skill"]
        run = self.create(plan)
        brief = self.store.brief(run, "code")
        self.assertLessEqual(brief["chars"], 16000)
        self.assertFalse(brief["spawned"])
        self.assertEqual(brief["execution"], "host_delegation_required")
        self.assertIn("write_paths", brief["delegation_prompt"])
        self.assertTrue(brief["missing_profiles"])

    def test_no_secret_file_read_by_cli_error(self):
        run = self.create()
        self.assertEqual(main(["--store", str(self.root / "runs"), "claim", "--run", run,
                               "--task", "package", "--actor", "worker"]), 2)


if __name__ == "__main__":
    unittest.main()
