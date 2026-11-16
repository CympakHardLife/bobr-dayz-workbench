"""Проверка локальных заявленных результатов; не запускает игру и не оценивает видео."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from common import checked_path, result, text_file, tree


SIDES = {"server", "client", "client_server"}
LIMIT = 128


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Повторный JSON key: " + key)
        value[key] = item
    return value


def document(path):
    return json.loads(text_file(path), object_pairs_hook=unique_object)


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("Неверные поля контракта: " + ", ".join(required))


def text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("Нужен непустой текст: " + name)
    return value


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Требуется lowercase SHA256")
    return value


def local_file(base, value):
    raw = Path(text(value, "path"))
    path = checked_path(raw if raw.is_absolute() else base / raw, exists=True)
    if not path.is_file():
        raise ValueError("Evidence/artifact должен быть файлом")
    return path


def file_hash(path):
    path = checked_path(path, exists=True)
    before = path.stat()
    identity = lambda state: (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns)
    value = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if identity(before) != identity(opened):
            raise ValueError("Файл изменился перед чтением SHA")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
        after_read = os.fstat(stream.fileno())
    after = checked_path(path, exists=True).stat()
    # В Windows stat и fstat могут возвращать разные виды ctime.
    # ctime сверяем только внутри одного механизма, identity — между ними.
    if (identity(before) != identity(after) or identity(opened) != identity(after_read)
            or before.st_ctime_ns != after.st_ctime_ns or opened.st_ctime_ns != after_read.st_ctime_ns):
        raise ValueError("Файл изменился при проверке SHA")
    return value.hexdigest()


def source_hash(root):
    """SHA256 canonical JSON [[relative POSIX path, file SHA256], ...], UTF-8."""
    root = checked_path(root, exists=True)
    paths = tree(root)
    if not paths:
        raise ValueError("Пустой source root")
    records = [[path.relative_to(root).as_posix(), file_hash(path)] for path in paths]
    # Повторный список/metadata ловит добавление, удаление и поздний drift при обходе.
    if paths != tree(root):
        raise ValueError("Набор source изменился при проверке")
    if any(file_hash(path) != row[1] for path, row in zip(paths, records)):
        raise ValueError("Source изменился при проверке")
    raw = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verified_files(rows, base):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("Нужно 1..16 evidence/artifact файлов")
    output, seen = [], set()
    for row in rows:
        fields(row, ("path", "sha256"))
        path = local_file(base, row["path"])
        key = str(path).casefold()
        if key in seen:
            raise ValueError("Повторный evidence/artifact path")
        seen.add(key)
        expected = digest(row["sha256"])
        if file_hash(path) != expected:
            raise ValueError("SHA drift evidence/artifact: " + str(path))
        output.append({"path": str(path), "sha256": expected})
    return output


def verify_build(build, base):
    fields(build, ("id", "source", "pbos"))
    text(build["id"], "build.id")
    fields(build["source"], ("root", "sha256"))
    raw_root = Path(text(build["source"]["root"], "source.root"))
    root = checked_path(raw_root if raw_root.is_absolute() else base / raw_root, exists=True)
    if source_hash(root) != digest(build["source"]["sha256"]):
        raise ValueError("Source SHA drift")
    pbos = verified_files(build["pbos"], base)
    if any(Path(row["path"]).suffix.lower() != ".pbo" for row in pbos):
        raise ValueError("Build pbos должны иметь расширение .pbo")
    return {"id": build["id"], "source_sha256": build["source"]["sha256"], "pbos": pbos}


def validate(spec_path, results_path):
    spec_path = checked_path(spec_path, exists=True)
    results_path = checked_path(results_path, exists=True)
    spec, observed = document(spec_path), document(results_path)
    fields(spec, ("schema_version", "build", "scenarios"))
    fields(observed, ("schema_version", "build", "scenarios"))
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1 or type(observed["schema_version"]) is not int or observed["schema_version"] != 1:
        raise ValueError("Поддерживается schema_version=1")
    if spec["build"] != observed["build"]:
        raise ValueError("Результаты относятся к другой сборке")
    build = verify_build(spec["build"], spec_path.parent)
    if not isinstance(spec["scenarios"], list) or not 1 <= len(spec["scenarios"]) <= LIMIT:
        raise ValueError("Нужно 1..128 ожидаемых сценариев")
    expected = {}
    for item in spec["scenarios"]:
        fields(item, ("id", "side", "expected"))
        ident = text(item["id"], "scenario.id")
        if ident in expected or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", ident):
            raise ValueError("Повторный или неверный scenario ID")
        if item["side"] not in SIDES:
            raise ValueError("Неверная scenario side")
        text(item["expected"], "expected result")
        expected[ident] = item
    if not isinstance(observed["scenarios"], list) or len(observed["scenarios"]) > LIMIT:
        raise ValueError("Неверный список результатов")
    supplied = {}
    for item in observed["scenarios"]:
        fields(item, ("id", "side", "events"))
        ident = text(item["id"], "result.id")
        if ident not in expected or ident in supplied:
            raise ValueError("Чужой или повторный scenario ID")
        if item["side"] != expected[ident]["side"]:
            raise ValueError("Результат получен для другой side")
        supplied[ident] = item
    outcomes = []
    for ident, item in expected.items():
        events = supplied.get(ident, {}).get("events", [])
        if not isinstance(events, list) or len(events) > 2:
            raise ValueError("Ожидаются START и один terminal event")
        outcome = {"id": ident, "side": item["side"], "expected": item["expected"], "status": "not_run", "evidence": []}
        if events:
            fields(events[0], ("event",))
            if events[0]["event"] != "START":
                raise ValueError("Terminal без START")
            outcome.update(status="fail", reason="started_without_terminal")
        if len(events) == 2:
            terminal = events[1]
            fields(terminal, ("event", "actual", "evidence"))
            if terminal["event"] not in {"PASS", "FAIL"}:
                raise ValueError("Повторный START или неверный terminal")
            actual = text(terminal["actual"], "actual result")
            evidence = verified_files(terminal["evidence"], results_path.parent)
            passed = terminal["event"] == "PASS" and actual == item["expected"]
            outcome.update(status="pass" if passed else "fail", actual=actual, evidence=evidence,
                           reason="reported_pass_expected_matches" if passed else "reported_fail_or_expected_mismatch")
        outcomes.append(outcome)
    status = "fail" if any(row["status"] == "fail" for row in outcomes) else "not_run" if any(row["status"] == "not_run" for row in outcomes) else "pass"
    # Проверяем build повторно: evidence hashing не должно скрыть source/PBO drift.
    verify_build(spec["build"], spec_path.parent)
    # Source rehash может занять время: evidence сверяем повторно перед возвратом.
    for outcome in outcomes:
        if outcome["evidence"]:
            verified_files(outcome["evidence"], results_path.parent)
    return result(status, ["current_source_and_PBO_sha256", "scenario_ids_side_lifecycle", "local_evidence_sha256", "reported_expected_result"],
                  skipped=["game_launch", "truth_of_observer_claim", "automatic_behavior_observation", "visual_baseline", "network"],
                  build=build, scenarios=outcomes, interpretation="validated_reported_results_only")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--results", required=True)
    args = parser.parse_args(argv)
    try:
        value = validate(args.spec, args.results)
    except (ValueError, OSError, TypeError, KeyError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
