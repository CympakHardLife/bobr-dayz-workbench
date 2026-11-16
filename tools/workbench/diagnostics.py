"""Адресные проверки DayZ: ресурсы, API, логи, выпуск и планы запуска.

Это самостоятельные инструменты Бобра. Они не запускают игру, сервер или сеть.
    python diagnostics.py --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

from common import AGENT, atomic_json, checked_path, load_config, now, read_json, redact, result, sha, source_bytes, text_file, tree


TEXT_SUFFIXES = {".c", ".cpp", ".hpp", ".rvmat", ".layout", ".xml", ".json", ".txt", ".md"}
RESOURCE_SUFFIXES = {".ogg", ".wav", ".paa", ".p3d", ".rvmat", ".imageset", ".layout", ".ptc", ".emat"}


def resources(source):
    root = checked_path(source, exists=True)
    files = tree(root)
    prefix_path = root / "$PBOPREFIX$"
    prefix = text_file(prefix_path).strip().replace("\\", "/") if prefix_path.is_file() else root.name
    names = {p.relative_to(root).as_posix().lower(): p for p in files}
    findings, external, signatures = [], set(), []
    for path in files:
        relative = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        if suffix in {".ogg", ".wav", ".p3d"}:
            with path.open("rb") as stream:
                header = stream.read(32)
            expected = {".ogg": (b"OggS",), ".wav": (b"RIFF",), ".p3d": (b"MLOD", b"ODOL")}[suffix]
            valid = any(header.startswith(item) for item in expected)
            if suffix == ".wav":
                valid = valid and header[8:12] == b"WAVE"
            signatures.append({"file": relative, "format": header[:4].decode("ascii", "replace"), "valid": valid})
            if not valid:
                findings.append({"code": "resource_header", "file": relative})
        if suffix in {".c", ".cpp", ".hpp", ".rvmat", ".layout", ".imageset"} and path.stat().st_size < 2 * 1024 * 1024:
            content = strip_comments(text_file(path))
            for match in re.finditer(r'"([^"\r\n]+\.(?:ogg|wav|paa|p3d|rvmat|imageset|layout|ptc|emat))"', content, re.I):
                ref = match.group(1).replace("\\", "/").lstrip("/")
                local = ref
                if ref.lower().startswith(prefix.lower() + "/"):
                    local = ref[len(prefix) + 1:]
                elif "/" in ref and ref.lower() not in names:
                    external.add(ref)
                    continue
                if ".." in Path(local).parts or local.lower() not in names:
                    findings.append({"code": "missing_local_resource", "file": relative,
                                     "line": content.count("\n", 0, match.start()) + 1, "reference": ref})
    return result("fail" if findings else "pass", ["local_resource_references", "ogg_wav_p3d_headers"],
                  skipped=["external_prefix_resolution", "decoder_playback", "P3D_geometry_LOD", "runtime_sound_attenuation"],
                  findings=findings, external_references=sorted(external), signatures=signatures, source=str(root))


def layout_audit(source):
    root = checked_path(source, exists=True)
    files = [root] if root.is_file() else tree(root, {".layout"})
    if not files:
        return result("not_run", [], skipped=["no_layout_files"])
    findings = []
    for path in files:
        content = text_file(path)
        clean = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', '""', content, flags=re.S)
        depth = 0
        for char in clean:
            depth += (char == "{") - (char == "}")
            if depth < 0:
                break
        if depth != 0:
            findings.append({"code": "unbalanced_braces", "file": str(path)})
        for match in re.finditer(r"(?m)^\s*size\s+(-?[\d.]+)\s+(-?[\d.]+)", clean):
            try:
                size = [float(match.group(1)), float(match.group(2))]
            except ValueError:
                findings.append({"code": "invalid_size", "file": str(path)})
                continue
            if min(size) <= 0:
                findings.append({"code": "non_positive_size", "file": str(path), "size": size,
                                 "line": clean.count("\n", 0, match.start()) + 1})
    return result("fail" if findings else "pass", ["layout_brace_balance", "literal_positive_sizes"],
                  findings=findings, files=len(files), skipped=["widget_runtime_types", "nested_anchors", "DPI", "visual_render"])


def strip_comments(content):
    return re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
                  lambda m: m.group(0) if m.group(0).startswith('"') else "\n" * m.group(0).count("\n"),
                  content, flags=re.S)


def api_snapshot(source, output, label):
    root = checked_path(source, exists=True)
    output = checked_path(output)
    if output.exists():
        raise ValueError("Снимок уже существует; укажи новый путь")
    records, files = [], {}
    class_pattern = re.compile(r"(?m)^\s*((?:modded\s+|abstract\s+)*)class\s+(\w+)(?:\s+extends\s+(\w+))?")
    method_pattern = re.compile(r"(?m)^[ \t]*(?:(?:proto|native|static|override|private|protected|public|final)\s+)*(?:[\w<>,]+[ \t]+)+(\w+)[ \t]*\([^;{}]*\)\s*(?:;|\{)")
    for path in tree(root, {".c", ".cpp", ".hpp"}):
        relative = path.relative_to(root).as_posix()
        raw = source_bytes(path)
        content = strip_comments(raw.decode("utf-8-sig"))
        files[relative] = hashlib.sha256(raw).hexdigest()
        for kind, pattern in (("class", class_pattern), ("method", method_pattern)):
            for match in pattern.finditer(content):
                declaration = re.sub(r"\s+", " ", match.group(0)).strip().rstrip("{;").strip()
                records.append({"kind": kind, "file": relative,
                                "line": content.count("\n", 0, match.start()) + 1,
                                "declaration": declaration})
    value = {"schema_version": 1, "created_utc": now(), "label": label, "source": str(root),
             "files": files, "records": records,
             "limitations": ["lexical_declarations_not_Enforce_parser", "guards_not_evaluated", "load_order_not_modelled", "native_behavior_not_in_source"]}
    atomic_json(output, value)
    return result("pass", ["file_sha256", "class_and_method_declarations_including_overloads"],
                  skipped=value["limitations"], snapshot=str(output), files=len(files), declarations=len(records))


def api_diff(before, after):
    first, second = read_json(before), read_json(after)
    if first.get("schema_version") != 1 or second.get("schema_version") != 1:
        raise ValueError("Неверный формат снимка API")
    def identities(snapshot):
        return {(row["kind"], row["file"], row["declaration"]) for row in snapshot["records"]}
    old, new = identities(first), identities(second)
    changed = sorted(name for name in set(first["files"]) | set(second["files"])
                     if first["files"].get(name) != second["files"].get(name))
    return result("pass", ["declaration_set_diff", "file_body_hash_diff"],
                  skipped=["semantic_compatibility", "preprocessor_configuration", "runtime_regression"],
                  removed=[list(item) for item in sorted(old - new)],
                  added=[list(item) for item in sorted(new - old)], changed_files=changed,
                  needs_review=bool(changed), labels=[first.get("label"), second.get("label")])


def logs(path, cursor=None, limit=100):
    path = checked_path(path, exists=True)
    if not 1 <= limit <= 500:
        raise ValueError("limit: 1..500")
    state = read_json(cursor) if cursor and Path(cursor).is_file() else {}
    info = path.stat()
    size = info.st_size
    with path.open("rb") as stream:
        prefix_length = min(state.get("prefix_length", 128), size)
        prefix = hashlib.sha256(stream.read(prefix_length)).hexdigest()
        offset = state.get("offset", 0) if state.get("path") == str(path) else 0
        if (state.get("prefix") != prefix or type(offset) is not int or offset < 0 or offset > size
                or state.get("file_id") != [info.st_dev, info.st_ino]):
            offset = 0
        start = max(offset, size - 1024 * 1024)
        stream.seek(0)
        next_prefix_length = min(128, size)
        next_prefix = hashlib.sha256(stream.read(next_prefix_length)).hexdigest()
        stream.seek(start)
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Лог вырос слишком быстро; повтори адресный запрос")
    complete = raw.rfind(b"\n") + 1 if cursor else len(raw)
    lines = raw[:complete].decode("utf-8", "replace").splitlines()
    matches = []
    for line in lines:
        if re.search(r"(?i)(error|exception|can't compile|cannot compile|undefined|crash|warning)", line):
            matches.append(redact(line)[:1500])
    if cursor:
        if checked_path(cursor) == path:
            raise ValueError("Cursor не должен перезаписывать лог")
        atomic_json(cursor, {"path": str(path), "offset": start + complete, "prefix": next_prefix,
                             "prefix_length": next_prefix_length, "file_id": [info.st_dev, info.st_ino], "updated_utc": now()})
    return result("findings" if matches else "pass", ["selected_log_new_bytes", "error_warning_patterns"],
                  skipped=["errors_without_log_messages", "causal_diagnosis", "tail_before_1MiB" if start > offset else "none"],
                  findings=matches[-limit:], total_matches=len(matches), truncated=len(matches) > limit,
                  bytes_from=start, bytes_to=start + complete, partial_line_bytes=len(raw) - complete, source=str(path))


def release(files, output):
    output = checked_path(output)
    if output.exists() or output.suffix.lower() != ".zip":
        raise ValueError("Нужен новый ZIP-файл")
    allowed = {".pbo", ".bikey", ".md", ".txt", ".json", ".xml", ".csv"}
    paths, names = [], set()
    for value in files:
        path = checked_path(value, exists=True)
        if not path.is_file() or path.suffix.lower() not in allowed:
            raise ValueError("В релиз включаются только явно перечисленные PBO/документы/public bikey")
        if path.name.lower() in names or path.name.lower() == "manifest.json":
            raise ValueError("Повтор имени в пакете")
        if path.suffix.lower() == ".pbo":
            import pipeline
            inspected = pipeline.inspect_pbo(path)
            with path.open("rb") as stream:
                for entry in inspected["entries"]:
                    if Path(entry["name"]).suffix.lower() not in TEXT_SUFFIXES | {".cfg"}:
                        continue
                    if entry["packing"] != 0 or entry["size"] > 8 * 1024 * 1024:
                        raise ValueError("Текстовый PBO entry требует отдельной проверки перед release: " + entry["name"])
                    stream.seek(entry["offset"])
                    content = stream.read(entry["size"]).decode("utf-8-sig", "replace")
                    if redact(content) != content or "PRIVATE KEY-----" in content:
                        raise ValueError("Приватные данные внутри PBO: " + entry["name"])
        if path.suffix.lower() in {".json", ".xml", ".txt", ".md", ".csv"}:
            content = text_file(path)
            if redact(content) != content or "PRIVATE KEY-----" in content:
                raise ValueError("В текстовом файле обнаружены приватные данные: " + path.name)
        names.add(path.name.lower())
        paths.append(path)
    if not paths:
        raise ValueError("Пустой релиз не создаётся")
    manifest = {"schema_version": 1, "created_utc": now(), "files": {p.name: sha(p) for p in paths},
                "signed_by_tool": False, "runtime_validation": "not_claimed"}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".partial")
    if temporary.exists():
        raise ValueError("Остался незавершённый ZIP; проверь его отдельно")
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                archive.write(path, path.name)
            archive.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=4))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip():
                raise ValueError("CRC архива не совпал")
            for name, expected in manifest["files"].items():
                if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                    raise ValueError("Исходник изменился во время упаковки")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return result("pass", ["explicit_payload_allowlist", "zip_crc", "payload_sha256"],
                  skipped=["publication", "signing", "game_test"], archive=str(output), sha256=sha(output), files=len(paths))


def texture(source, output, tools, run=False):
    source, output = checked_path(source, exists=True), checked_path(output)
    tool = checked_path(Path(tools) / "Bin/ImageToPAA/ImageToPAA.exe", exists=True)
    if source.suffix.lower() not in {".tga", ".png"} or output.suffix.lower() != ".paa":
        raise ValueError("Ожидается PNG/TGA → новый PAA")
    if output.exists():
        raise ValueError("Готовая текстура не перезаписывается")
    command = [str(tool), str(source), str(output)]
    if not run:
        return result("plan", ["conversion_command"], command=command, skipped=["conversion_not_executed"])
    output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(TEMP=str(output.parent), TMP=str(output.parent))
    process = subprocess.run(command, capture_output=True, timeout=120, env=env)
    valid = process.returncode == 0 and output.is_file() and output.stat().st_size > 8
    return result("pass" if valid else "fail", ["ImageToPAA_exit", "fresh_output"],
                  skipped=["visual_material_in_game"], exit_code=process.returncode,
                  output=str(output), log=redact((process.stdout + process.stderr).decode("utf-8", "replace"))[-3000:])


def session_plan(project, side, config):
    project = checked_path(project, exists=True)
    game = checked_path(config["game_root"])
    exe = game / "DayZDiag_x64.exe"
    # Это план для выделенного стенда. Конкретная mission и моды задаются до запуска человеком.
    return result("plan", ["client_server_side_separation"],
                  skipped=["game_launch", "mission_and_dependencies_not_selected", "Enforce_compilation", "runtime_behavior"],
                  executable=str(exe), executable_exists=exe.is_file(), project=str(project), side=side,
                  server_mod_argument="-serverMod=" + str(project) if side == "server" else "-mod=" + str(project),
                  client_mod_argument=None if side == "server" else "-mod=" + str(project),
                  required_scenarios=["clean_compile_and_mission", "join_and_RPC", "restart_persistence", "feature_success_and_cancel", "load_budget"],
                  notice="PBO-каталог мода и миссию выбрать перед запуском; source dir сам не является установленным модом.")


def config_check(source, output, tools, run=False):
    source = checked_path(source, exists=True)
    if source.is_dir():
        source = checked_path(source / "config.cpp", exists=True)
    output = checked_path(output)
    if output.exists() or output.suffix.lower() != ".bin":
        raise ValueError("Нужен новый config .bin вне исходников")
    if output.parent == source.parent:
        raise ValueError("Результат проверки не добавляется в исходный мод")
    tool = checked_path(Path(tools) / "Bin/CfgConvert/CfgConvert.exe", exists=True)
    command = [str(tool), "-bin", "-dst", str(output), str(source)]
    if not run:
        return result("plan", ["CfgConvert_command"], skipped=["config_conversion_not_executed"], command=command)
    source_hash = sha(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(TEMP=str(output.parent), TMP=str(output.parent))
    process = subprocess.run(command, capture_output=True, timeout=30, env=env, cwd=source.parent)
    log = (process.stdout + process.stderr).decode("utf-8", "replace")
    header = output.read_bytes()[:4] if output.is_file() else b""
    valid = process.returncode == 0 and header == b"\x00raP" and sha(source) == source_hash
    return result("pass" if valid else "fail", ["real_CfgConvert", "fresh_raP_output", "source_hash_unchanged"],
                  skipped=["Enforce_script_compilation", "external_CfgPatches_load_order", "game"],
                  exit_code=process.returncode, output=str(output), log=redact(log)[-3000:])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("resource-audit", "layout-audit"):
        p = subs.add_parser(name)
        p.add_argument("--source", required=True)
    p = subs.add_parser("api-snapshot")
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--label", required=True)
    p = subs.add_parser("api-diff")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p = subs.add_parser("logs")
    p.add_argument("--file", required=True)
    p.add_argument("--cursor")
    p.add_argument("--limit", type=int, default=100)
    p = subs.add_parser("release")
    p.add_argument("--file", action="append", required=True)
    p.add_argument("--output", required=True)
    p = subs.add_parser("texture")
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--tools", default=load_config()["tools_root"])
    p.add_argument("--run", action="store_true")
    p = subs.add_parser("session-plan")
    p.add_argument("--project", required=True)
    p.add_argument("--side", choices=("client", "server"), required=True)
    p = subs.add_parser("config-check")
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--tools", default=load_config()["tools_root"])
    p.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    try:
        command = args.command
        if command == "resource-audit": value = resources(args.source)
        elif command == "layout-audit": value = layout_audit(args.source)
        elif command == "api-snapshot": value = api_snapshot(args.source, args.output, args.label)
        elif command == "api-diff": value = api_diff(args.before, args.after)
        elif command == "logs": value = logs(args.file, args.cursor, args.limit)
        elif command == "release": value = release(args.file, args.output)
        elif command == "texture": value = texture(args.source, args.output, args.tools, args.run)
        elif command == "config-check": value = config_check(args.source, args.output, args.tools, args.run)
        else: value = session_plan(args.project, args.side, load_config())
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 1 if value["status"] == "fail" else 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
