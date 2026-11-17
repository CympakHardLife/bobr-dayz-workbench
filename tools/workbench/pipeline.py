"""Бобр: подготовка, упаковка и проверяемая доставка собственных модов DayZ.

Все операции по умолчанию отделены от запуска игры и подписи. build/deploy
сначала печатают план; --run исполняет именно указанную операцию.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid


VERSION = "2.0.5"
AGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOOLS = None
WRITE_ROOT_KEYS = ("workspace_root", "test_workspace", "runs_root", "reports_root", "builds_root", "temp_root")
REPARSE = 0x400
MAX_FILES = 50000
MAX_TEXT = 8 * 1024 * 1024
MAX_PBO = 16 * 1024 * 1024 * 1024
VERS = 0x56657273
CPRS = 0x43707273
SECRET_NAMES = {".env", "credentials.json", "secrets.json", "secrets.yaml"}
PRIVATE_SUFFIXES = {".biprivatekey", ".pem", ".pfx", ".p12", ".key"}
IGNORED_DIRS = {".git", ".svn", ".hg", "__pycache__", ".pytest_cache"}
PACKAGE_SUFFIXES = {
    ".bin", ".c", ".cpp", ".hpp", ".h", ".ogg", ".wav", ".wss", ".paa",
    ".p3d", ".rvmat", ".emat", ".edds", ".ptc", ".layout", ".imageset",
    ".csv", ".xml", ".json", ".rtm", ".fnt", ".wrp", ".fsm", ".lip",
}
SENSITIVE_TEXT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:password|passwordAdmin|api_key|access_token|secret_key)[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{8,})[\"']",
    re.IGNORECASE,
)


class PipelineError(ValueError):
    pass


def runtime_config():
    """Читать только локальный конфиг; отдельный путь удобен для CI/fixtures."""
    configured = os.environ.get("BOBR_WORKBENCH_CONFIG")
    path = Path(configured) if configured else AGENT_ROOT / "workbench/config.json"
    path = checked_path(path)
    if not path.is_file():
        return {}
    value = json.loads(read_text(path))
    if not isinstance(value, dict):
        raise PipelineError("Конфигурация Workbench должна быть JSON-объектом")
    return value


def configured_path(value):
    if not isinstance(value, str) or not value.strip():
        raise PipelineError("Зарегистрированный путь должен быть непустой строкой")
    path = Path(value)
    return checked_path(path if path.is_absolute() else AGENT_ROOT / path)


def approved_write_roots():
    config = runtime_config()
    roots = [configured_path(config[key]) for key in WRITE_ROOT_KEYS if config.get(key)]
    extra = config.get("approved_write_roots", [])
    if not isinstance(extra, list) or len(extra) > 128:
        raise PipelineError("approved_write_roots должен быть списком до 128 путей")
    roots.extend(configured_path(value) for value in extra)
    return roots


def deployment_registered(target):
    targets = runtime_config().get("deployment_targets", [])
    if not isinstance(targets, list) or len(targets) > 128:
        raise PipelineError("deployment_targets должен быть списком до 128 точных каталогов")
    return any(target == configured_path(value) for value in targets)


def checked_path(value, *, exists=False, directory=None, write=False):
    """Не переходить через junction/symlink или сетевой путь при работе с модом."""
    text = os.fspath(value)
    if not text or any(c in text for c in ("\x00", "\r", "\n")):
        raise PipelineError("Некорректный путь")
    path = Path(text).absolute()
    if ".." in path.parts or path.drive.startswith("\\"):
        raise PipelineError("Обратные переходы и сетевые пути не разрешены")
    for item in (path, *path.parents):
        try:
            stat = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(stat, "st_file_attributes", 0) & REPARSE:
            raise PipelineError("Путь проходит через symlink/junction: " + str(item))
    if exists and not path.exists():
        raise PipelineError("Путь не существует: " + str(path))
    if exists and directory is True and not path.is_dir():
        raise PipelineError("Ожидался каталог: " + str(path))
    if exists and directory is False and not path.is_file():
        raise PipelineError("Ожидался файл: " + str(path))
    if write and not any(path.is_relative_to(root) for root in approved_write_roots()):
        raise PipelineError("Каталог записи не зарегистрирован в конфигурации Workbench: " + str(path))
    return path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path):
    if path.stat().st_size > MAX_TEXT:
        raise PipelineError("Слишком большой текстовый файл: " + str(path))
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_TEXT + 1)
        if len(data) > MAX_TEXT:
            raise PipelineError("Текст вырос сверх лимита чтения: " + str(path))
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PipelineError("Текст не в UTF-8: " + str(path)) from error


def secret_path(relative):
    parts = [part.casefold() for part in Path(relative).parts]
    return (
        any(part in {"secrets", "keys", ".ssh"} for part in parts)
        or Path(relative).name.casefold() in SECRET_NAMES
        or Path(relative).suffix.casefold() in PRIVATE_SUFFIXES
        or Path(relative).name.casefold().startswith(".env.")
    )


def source_files(source):
    source = checked_path(source, exists=True, directory=True)
    found = []
    for current, directories, files in os.walk(source, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in IGNORED_DIRS)
        for name in directories:
            checked_path(Path(current) / name, exists=True, directory=True)
            if secret_path((Path(current) / name).relative_to(source)):
                raise PipelineError("Приватный каталог не допускается в исходниках пакета")
        for name in sorted(files):
            path = checked_path(Path(current) / name, exists=True, directory=False)
            relative = path.relative_to(source)
            if secret_path(relative):
                raise PipelineError("Приватный файл не допускается в пакете: " + str(relative))
            found.append(path)
            if len(found) > MAX_FILES:
                raise PipelineError("Превышено число файлов исходников")
    return found


def package_files(source):
    result = []
    for path in source_files(source):
        if path.name == "$PBOPREFIX$" or path.suffix.lower() in PACKAGE_SUFFIXES:
            if path.suffix.lower() in {".c", ".cpp", ".hpp", ".h", ".json", ".xml", ".csv"}:
                if SENSITIVE_TEXT.search(read_text(path)):
                    raise PipelineError("В исходном тексте обнаружены возможные секреты: " + str(path.name))
            result.append(path)
    return result


def prefix_for(source):
    marker = Path(source) / "$PBOPREFIX$"
    return normalize_prefix(read_text(marker) if marker.exists() else Path(source).name)


def normalize_prefix(value):
    # FileBank/BankRev допускают завершающий разделитель в prefix metadata.
    prefix = value.strip().replace("\\", "/").rstrip("/")
    try:
        return _entry_path(prefix)
    except PipelineError as error:
        raise PipelineError("Недопустимый PBO prefix") from error


def strip_comments(text):
    return re.sub(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\r\n]*',
                  lambda m: m.group(0) if m.group(0).startswith('"') else " " * len(m.group(0)),
                  text, flags=re.DOTALL)


def brace_errors(text):
    clean = re.sub(r'"(?:\\.|[^"\\])*"', '""', strip_comments(text))
    stack = []
    for number, char in enumerate(clean):
        if char in "{[":
            stack.append((char, number))
        elif char in "}]":
            if not stack or stack.pop()[0] != {"}": "{", "]": "["}[char]:
                return ["Нарушен баланс фигурных/квадратных скобок"]
    return ["Незакрытая скобка"] if stack else []


def class_block(text, name):
    match = re.search(r"\bclass\s+" + re.escape(name) + r"\s*(?::[^{};]+)?\s*\{", text)
    if not match:
        return None
    level = 1
    position = match.end()
    quote = False
    escaped = False
    for end in range(position, len(text)):
        char = text[end]
        if char == '"' and not escaped:
            quote = not quote
        if not quote:
            if char == "{":
                level += 1
            elif char == "}":
                level -= 1
                if not level:
                    return text[position:end]
        escaped = char == "\\" and not escaped
    return None


def lint(source):
    source = checked_path(source, exists=True, directory=True)
    all_files = source_files(source)
    packaged = package_files(source)
    findings = []
    skipped = ["Enforce-компиляция", "проверка API/типов", "игровое поведение", "полный граф внешних CfgPatches"]
    prefix = prefix_for(source)
    config = source / "config.cpp"
    if not config.is_file():
        if (source / "config.bin").is_file():
            skipped.append("Разбор бинарного config.bin")
        else:
            findings.append({"severity": "error", "path": "config.cpp", "message": "Нет config.cpp или config.bin"})
    else:
        raw = read_text(config)
        clean = strip_comments(raw)
        for message in brace_errors(raw):
            findings.append({"severity": "error", "path": "config.cpp", "message": message})
        patches = class_block(clean, "CfgPatches")
        mods = class_block(clean, "CfgMods")
        if patches is None:
            findings.append({"severity": "error", "path": "config.cpp", "message": "Нет CfgPatches"})
        elif not re.search(r"\brequiredAddons\s*\[\s*\]\s*=\s*\{", patches):
            findings.append({"severity": "error", "path": "config.cpp", "message": "Нет массива requiredAddons[]"})
        scripts = [p for p in packaged if p.suffix.lower() == ".c"]
        if scripts and mods is None:
            findings.append({"severity": "error", "path": "config.cpp", "message": "Есть скрипты, но нет CfgMods"})
        if mods is not None:
            for module in ("gameScriptModule", "worldScriptModule", "missionScriptModule"):
                block = class_block(mods, module)
                if block is None:
                    continue
                files_match = re.search(r"\bfiles\s*\[\s*\]\s*=\s*\{([^}]+)\}", block)
                if not files_match:
                    findings.append({"severity": "error", "path": "config.cpp", "message": module + ": нет непустого files[]"})
                    continue
                for file_path in re.findall(r'"([^"\r\n]+)"', files_match.group(1)):
                    normalized = file_path.replace("\\", "/").lstrip("/")
                    try:
                        _entry_path(normalized)
                    except PipelineError:
                        findings.append({"severity": "error", "path": "config.cpp", "message": "Недопустимый script path"})
                        continue
                    if not normalized.casefold().startswith(prefix.casefold() + "/"):
                        skipped.append("Внешний script path: " + normalized)
                        continue
                    relative = normalized[len(prefix) + 1:]
                    pieces = PurePosixPath(relative).parts
                    if ".." in pieces or ":" in relative or relative.startswith("/"):
                        findings.append({"severity": "error", "path": "config.cpp", "message": "Выход script path из prefix"})
                    elif not (source / Path(*pieces)).is_dir():
                        findings.append({"severity": "error", "path": "config.cpp", "message": "Нет script directory: " + relative})
        for resource in re.findall(r'"([^"\r\n]+\.(?:p3d|paa|rvmat|ogg|wss|wav|layout|imageset))"', clean, re.IGNORECASE):
            normalized = resource.replace("\\", "/").lstrip("/")
            try:
                _entry_path(normalized)
            except PipelineError:
                findings.append({"severity": "error", "path": "config.cpp", "message": "Недопустимый виртуальный путь ресурса"})
                continue
            if normalized.casefold().startswith(prefix.casefold() + "/"):
                relative = normalized[len(prefix) + 1:]
                if ".." in PurePosixPath(relative).parts or not (source / relative).is_file():
                    findings.append({"severity": "error", "path": "config.cpp", "message": "Нет собственного ресурса: " + relative})
            else:
                skipped.append("Внешний ресурс: " + normalized)
    return {
        "status": "failed" if any(f["severity"] == "error" for f in findings) else "partial" if not config.is_file() else "passed",
        "source": str(source), "prefix": prefix, "files_scanned": len(all_files),
        "package_files": len(packaged), "findings": findings,
        "coverage": ["пути и reparse", "приватные файлы/типовые секреты", "баланс config.cpp", "CfgPatches", "CfgMods script paths", "локальные ресурсы config.cpp"],
        "skipped": sorted(set(skipped)),
    }


def preflight(tools=DEFAULT_TOOLS, source=None):
    config = runtime_config()
    if tools is not None:
        tools = checked_path(tools)
    else:
        tools = configured_path(config["tools_root"]) if config.get("tools_root") else None
    programs = {
        "AddonBuilder": "Bin/AddonBuilder/AddonBuilder.exe",
        "CfgConvert": "Bin/CfgConvert/CfgConvert.exe",
        "ImageToPAA": "Bin/ImageToPAA/ImageToPAA.exe",
        "FileBank": "Bin/PboUtils/FileBank.exe",
        "WorkDrive": "Bin/WorkDrive/WorkDrive.exe",
    }
    available = {name: {"path": str(tools / relative) if tools else None, "available": bool(tools and (tools / relative).is_file())} for name, relative in programs.items()}
    workdrive = configured_path(config["workdrive"]) if config.get("workdrive") else None
    result = {
        "status": "passed" if available["AddonBuilder"]["available"] else "partial",
        "tools": available, "workdrive": str(workdrive) if workdrive else None,
        "workdrive_present": bool(workdrive and workdrive.is_dir()), "builtin_pack_available": True,
        "coverage": ["наличие настроенных tools", "доступность настроенного workdrive"],
        "skipped": ["tools не запускались", "workdrive не монтировался", "версия игры не установлена", "Enforce-компиляция"],
    }
    if source:
        result["source"] = lint(source)
        if result["source"]["status"] == "failed":
            result["status"] = "failed"
    return result


def scaffold(workspace, name, side="client"):
    workspace = checked_path(workspace, exists=True, directory=True, write=True)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,63}", name):
        raise PipelineError("Имя мода: 2–64 ASCII буквы/цифры/_, первая буква")
    if side not in {"client", "server"}:
        raise PipelineError("side должен быть client или server")
    target = checked_path(workspace / name, write=True)
    if target.exists():
        raise PipelineError("Существующий мод не перезаписывается")
    target.mkdir()
    for layer in ("3_Game", "4_World", "5_Mission"):
        (target / "Scripts" / layer).mkdir(parents=True)
    config = f'''class CfgPatches
{{
    class {name}
    {{
        requiredVersion = 0.1;
        requiredAddons[] = {{"DZ_Data"}};
        units[] = {{}};
        weapons[] = {{}};
    }};
}};

class CfgMods
{{
    class {name}
    {{
        dir = "{name}";
        name = "{name}";
        type = "mod";
        dependencies[] = {{"Game", "World", "Mission"}};
        class defs
        {{
            class gameScriptModule
            {{
                value = "";
                files[] = {{"{name}/Scripts/3_Game"}};
            }};
            class worldScriptModule
            {{
                value = "";
                files[] = {{"{name}/Scripts/4_World"}};
            }};
            class missionScriptModule
            {{
                value = "";
                files[] = {{"{name}/Scripts/5_Mission"}};
            }};
        }};
    }};
}};
'''
    (target / "config.cpp").write_text(config, encoding="utf-8", newline="\n")
    (target / "$PBOPREFIX$").write_text(name + "\n", encoding="utf-8")
    (target / "Scripts/3_Game" / (name + "_BuildInfo.c")).write_text(
        f'class {name}_BuildInfo\n{{\n    static string Version = "0.1.0";\n}}\n', encoding="utf-8", newline="\n")
    (target / "README.md").write_text(
        f"# {name}\n\nСобственный мод DayZ.\n\n"
        f"## Установка\n\nСторона загрузки: {side}. "
        + ("Параметр сервера -serverMod. Клиентская часть автоматически не создаётся.\n" if side == "server" else "Параметр -mod на клиенте и сервере по контракту мода.\n")
        + "\n## Использование\n\nКаркас не содержит игровой механики. Добавляй её после проверки API и стороны.\n",
        encoding="utf-8", newline="\n")
    return {"status": "passed", "source": str(target), "side": side,
            "coverage": ["создан новый каркас CfgPatches/CfgMods и script layers"],
            "skipped": ["новая механика", "Enforce-компиляция", "упаковка и deployment"], "lint": lint(target)}


def _cstring(stream, limit=4096):
    data = bytearray()
    for _ in range(limit + 1):
        char = stream.read(1)
        if not char:
            raise PipelineError("Обрезанная PBO-строка")
        if char == b"\0":
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise PipelineError("PBO-имя не в UTF-8") from error
        data.extend(char)
    raise PipelineError("PBO-строка превышает лимит")


def _entry_path(name):
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if not name or normalized.startswith("/") or ":" in normalized or any(p in {"", ".", ".."} for p in normalized.split("/")) or any(ord(c) < 32 for c in name):
        raise PipelineError("Недопустимое имя PBO entry")
    if secret_path(Path(*parts)):
        raise PipelineError("Приватный файл внутри PBO")
    return normalized


def inspect_pbo(path):
    path = checked_path(path, exists=True, directory=False)
    if path.suffix.casefold() != ".pbo":
        raise PipelineError("Ожидался .pbo")
    before = path.stat()
    size = before.st_size
    if size < 21 or size > MAX_PBO:
        raise PipelineError("Недопустимый размер PBO")
    entries = []
    properties = {}
    names = set()
    with path.open("rb") as stream:
        version_seen = False
        for _ in range(MAX_FILES + 2):
            name = _cstring(stream)
            raw = stream.read(20)
            if len(raw) != 20:
                raise PipelineError("Обрезанный PBO header")
            packing, original, reserved, timestamp, length = struct.unpack("<5I", raw)
            if not name and packing == VERS:
                if version_seen or entries:
                    raise PipelineError("Повторный/поздний PBO version header")
                version_seen = True
                for _ in range(1024):
                    key = _cstring(stream)
                    if not key:
                        break
                    value = _cstring(stream)
                    if key.casefold() in {k.casefold() for k in properties}:
                        raise PipelineError("Повторное свойство PBO")
                    properties[key] = value
                else:
                    raise PipelineError("Слишком много PBO properties")
                continue
            if not name:
                if any((packing, original, reserved, timestamp, length)):
                    raise PipelineError("Некорректный PBO terminator")
                break
            normalized = _entry_path(name)
            if normalized.casefold() in names:
                raise PipelineError("Коллизия имён PBO entries")
            names.add(normalized.casefold())
            if packing not in {0, CPRS}:
                raise PipelineError("Неизвестный способ сжатия PBO")
            if packing == 0 and original not in {0, length}:
                raise PipelineError("Несогласованные размеры несжатого PBO entry")
            if len(entries) >= MAX_FILES:
                raise PipelineError("Слишком много PBO entries")
            entries.append({"name": normalized, "packing": packing, "size": length, "original_size": original})
        else:
            raise PipelineError("Слишком много PBO entries")
        data_offset = stream.tell()
        payload_size = sum(entry["size"] for entry in entries)
        if data_offset + payload_size > size:
            raise PipelineError("Обрезанное содержимое PBO")
        offset = data_offset
        for entry in entries:
            entry["offset"] = offset
            stream.seek(offset)
            digest = hashlib.sha256()
            remaining = entry["size"]
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise PipelineError("PBO изменился во время чтения")
                digest.update(chunk)
                remaining -= len(chunk)
            entry["stored_sha256"] = digest.hexdigest()
            entry["content_sha256"] = digest.hexdigest() if entry["packing"] == 0 else None
            offset += entry["size"]
        stream.seek(offset)
        trailer = stream.read(22)
        checksum = "absent"
        if trailer:
            if len(trailer) != 21 or trailer[0] != 0:
                raise PipelineError("Неизвестный PBO trailer")
            stream.seek(0)
            digest = hashlib.sha1()
            remaining = offset
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise PipelineError("PBO изменился во время проверки SHA1")
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.digest() != trailer[1:]:
                raise PipelineError("Не совпадает PBO SHA1 trailer")
            checksum = "verified"
    prefix = next((value for key, value in properties.items() if key.casefold() == "prefix"), None)
    if prefix is not None:
        prefix = normalize_prefix(prefix)
    file_hash = sha256(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise PipelineError("PBO изменился во время проверки")
    return {"status": "passed", "path": str(path), "bytes": size, "sha256": file_hash,
            "prefix": prefix, "properties": properties, "entries": entries, "checksum": checksum,
            "coverage": ["PBO headers/bounds", "entry paths/collisions", "stored SHA256", "SHA1 trailer если присутствует"],
            "skipped": ["содержимое сжатых entries не декодируется", "Enforce-компиляция", "игровая загрузка"]}


def _disjoint(first, second):
    if first == second or first.is_relative_to(second) or second.is_relative_to(first):
        raise PipelineError("Исходники, output и temp должны быть раздельными каталогами")


def write_uncompressed_pbo(source, output, prefix):
    """Детерминированный pack-only PBO: без Binarize, компиляции и сжатия."""
    source = checked_path(source, exists=True, directory=True)
    output = checked_path(output, write=True)
    prefix = normalize_prefix(prefix)
    paths = [path for path in package_files(source) if path.name != "$PBOPREFIX$"]
    paths.sort(key=lambda path: (path.relative_to(source).as_posix().casefold(), path.relative_to(source).as_posix()))
    names = set()
    entries = []
    for path in paths:
        name = _entry_path(path.relative_to(source).as_posix())
        if name.casefold() in names:
            raise PipelineError("Коллизия имён в собственной PBO-сборке")
        names.add(name.casefold())
        size = path.stat().st_size
        if size > 0xFFFFFFFF:
            raise PipelineError("Один PBO entry превышает 32-битный формат размера")
        entries.append((name, path, size))
    if not entries:
        raise PipelineError("Пустой PBO не собирается")
    output.parent.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha1()
    with output.open("xb") as stream:
        def emit(data):
            stream.write(data)
            checksum.update(data)
        emit(b"\0" + struct.pack("<5I", VERS, 0, 0, 0, 0))
        emit(b"prefix\0" + prefix.replace("/", "\\").encode("utf-8") + b"\0\0")
        for name, path, size in entries:
            emit(name.replace("/", "\\").encode("utf-8") + b"\0")
            emit(struct.pack("<5I", 0, 0, 0, 0, size))
        emit(b"\0" + b"\0" * 20)
        for name, path, expected_size in entries:
            copied = 0
            with path.open("rb") as contents:
                for data in iter(lambda: contents.read(1024 * 1024), b""):
                    copied += len(data)
                    if copied > expected_size:
                        raise PipelineError("Размер entry вырос во время PBO-сборки: " + name)
                    emit(data)
            if copied != expected_size:
                raise PipelineError("Размер entry изменился во время PBO-сборки: " + name)
        stream.write(b"\0" + checksum.digest())
        stream.flush()
        os.fsync(stream.fileno())
    return inspect_pbo(output)


def build(source, output, tools=DEFAULT_TOOLS, temp=None, run=False, timeout=180, backend="builtin"):
    source = checked_path(source, exists=True, directory=True)
    output = checked_path(output, write=True)
    if backend not in {"builtin", "addonbuilder"}:
        raise PipelineError("backend должен быть builtin или addonbuilder")
    config = runtime_config()
    if tools is not None:
        tools = checked_path(tools, exists=backend == "addonbuilder", directory=True)
    else:
        tools = configured_path(config["tools_root"]) if config.get("tools_root") else None
        if tools is not None and backend == "addonbuilder":
            tools = checked_path(tools, exists=True, directory=True)
    if backend == "addonbuilder" and tools is None:
        raise PipelineError("Для addonbuilder укажи tools_root или --tools")
    if temp is None:
        temp = configured_path(config["temp_root"]) if config.get("temp_root") else None
    if not temp:
        raise PipelineError("Укажи --temp или настроенный temp_root; системный TEMP не выбирается автоматически")
    temp = checked_path(temp, write=True)
    for first, second in ((source, output), (source, temp), (output, temp)):
        _disjoint(first, second)
    for key in ("game_root", "server_root"):
        if config.get(key) and output.is_relative_to(configured_path(config[key])):
            raise PipelineError("build не пишет в установленную игру; используй отдельный deploy")
    check = lint(source)
    if check["status"] == "failed":
        return {"status": "failed", "reason": "lint", "lint": check, "coverage": check["coverage"], "skipped": check["skipped"]}
    executable = checked_path(tools / "Bin/AddonBuilder/AddonBuilder.exe", exists=True, directory=False) if backend == "addonbuilder" else None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,63}", source.name):
        raise PipelineError("Имя каталога PBO должно быть ASCII идентификатором")
    prefix = prefix_for(source)
    expected = output / (source.name + ".pbo")
    result = {"status": "planned", "source": str(source), "output": str(expected),
              "tool": str(executable) if executable else "stdlib uncompressed PBO writer", "backend": backend, "prefix": prefix, "pack_only": True,
              "coverage": ["lint", "план самостоятельной сборки без deployment/подписи"],
              "skipped": ["сборка не запускалась", "Binarize", "Enforce-компиляция", "игровая проверка"]}
    if not run:
        return result
    output.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="bobr-build-", dir=temp))
    stage = session / "source" / source.name
    stage.mkdir(parents=True)
    stage_output = session / "output"
    stage_output.mkdir()
    tool_temp = session / "temp"
    tool_temp.mkdir()
    include = session / "include.txt"
    include.write_text(";".join("*" + suffix for suffix in sorted(PACKAGE_SUFFIXES)), encoding="ascii")
    source_hashes = {}
    for original in package_files(source):
        relative = original.relative_to(source)
        staged = stage / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, staged)
        source_hashes[relative.as_posix()] = sha256(original)
        if sha256(staged) != source_hashes[relative.as_posix()]:
            raise PipelineError("Исходник изменился при staging")
    for layer in ("3_Game", "4_World", "5_Mission"):
        if (source / "Scripts" / layer).is_dir():
            (stage / "Scripts" / layer).mkdir(parents=True, exist_ok=True)
    built = stage_output / (source.name + ".pbo")
    command = []
    returncode = 0
    if backend == "builtin":
        write_uncompressed_pbo(stage, built, prefix)
        (session / "build.log").write_text("Builtin deterministic uncompressed PBO; no Binarize/Enforce/signing.\n", encoding="utf-8")
    else:
        command = [str(executable), str(stage), str(stage_output), "-packonly", "-prefix=" + prefix,
                   "-temp=" + str(tool_temp), "-include=" + str(include)]
        environment = os.environ.copy()
        environment["TEMP"] = environment["TMP"] = environment["TMPDIR"] = str(tool_temp)
        started = time.time()
        try:
            completed = subprocess.run(command, cwd=tools, env=environment, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            (session / "build.log").write_text("AddonBuilder started but exceeded the explicit timeout; no PBO promoted.\n", encoding="utf-8")
            return {**result, "status": "failed", "reason": "AddonBuilder timeout", "session": str(session),
                    "coverage": ["lint", "AddonBuilder запущен", "timeout", "готовый output не изменён"],
                    "skipped": ["сборка не завершена", "PBO readback", "Binarize", "Enforce-компиляция", "игровая проверка"],
                    "log": str(session / "build.log")}
        returncode = completed.returncode
        log = (completed.stdout + "\n" + completed.stderr)[-32768:]
        (session / "build.log").write_text(log, encoding="utf-8")
        if returncode != 0 or not built.is_file() or built.stat().st_mtime < started - 2:
            return {**result, "status": "failed", "reason": "AddonBuilder не создал свежий PBO", "returncode": returncode,
                    "session": str(session), "log": str(session / "build.log"),
                    "coverage": ["lint", "AddonBuilder запущен", "готовый output не изменён"],
                    "skipped": ["PBO readback", "Binarize", "Enforce-компиляция", "игровая проверка"]}
    inspected = inspect_pbo(built)
    if inspected["prefix"] is None or inspected["prefix"].replace("\\", "/") != prefix:
        raise PipelineError("PBO prefix не совпадает с исходниками")
    entries = {entry["name"].casefold(): entry for entry in inspected["entries"]}
    verified = []
    skipped_content = []
    current_sources = {path.relative_to(source).as_posix(): path for path in package_files(source)}
    if set(current_sources) != set(source_hashes):
        raise PipelineError("Состав исходников изменился во время сборки")
    expected_entries = {relative.casefold() for relative in source_hashes if relative != "$PBOPREFIX$"}
    converted_config = (backend == "addonbuilder" and "config.cpp" in expected_entries
                        and "config.bin" not in expected_entries and "config.cpp" not in entries
                        and "config.bin" in entries)
    generated_entries = []
    for name in sorted(set(entries) - expected_entries):
        entry = entries[name]
        reason = None
        if backend == "addonbuilder":
            if name == "texheaders.bin":
                reason = "AddonBuilder texture headers"
            elif name == "config.bin" and converted_config:
                reason = "AddonBuilder config.cpp conversion"
            elif (name == "config.cpp" and "config.bin" in expected_entries
                  and "config.cpp" not in expected_entries and entry["packing"] == 0
                  and entry["size"] == 0 and entry["original_size"] == 0):
                reason = "AddonBuilder empty config.cpp companion"
        if reason is None:
            raise PipelineError("Неожиданный PBO entry вне исходников: " + entry["name"])
        generated_entries.append({"name": entry["name"], "reason": reason, "content_verified": False})
        skipped_content.append("Семантика generated entry не проверена: " + entry["name"])
    for relative, digest in source_hashes.items():
        if relative == "$PBOPREFIX$":
            continue
        entry = entries.get(relative.casefold())
        if entry is None:
            if relative.casefold() == "config.cpp" and converted_config:
                skipped_content.append("config.cpp преобразован в config.bin")
                continue
            raise PipelineError("В PBO отсутствует исходный entry: " + relative)
        if entry["content_sha256"] is not None:
            if entry["content_sha256"] != digest:
                raise PipelineError("Содержимое PBO не совпадает со stage: " + relative)
            verified.append(relative)
        else:
            skipped_content.append("Сжатый entry: " + relative)
    for relative, digest in source_hashes.items():
        if sha256(source / relative) != digest:
            raise PipelineError("Исходник изменился во время сборки: " + relative)
    temporary = output / (expected.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        shutil.copyfile(built, temporary)
        if sha256(temporary) != inspected["sha256"]:
            raise PipelineError("Сборка изменилась при копировании")
        os.replace(temporary, expected)
    finally:
        if temporary.exists():
            temporary.unlink()
    receipt = {
        "status": "passed", "source": str(source), "output": str(expected), "sha256": inspected["sha256"],
        "prefix": prefix, "command": command, "returncode": returncode, "backend": backend, "pack_only": True,
        "compilation": {"Enforce": "not_run", "Binarize": "not_run", "config_conversion": "not_run" if backend == "builtin" else "not_claimed"},
        "session": str(session), "source_hashes": source_hashes, "content_verified": verified,
        "generated_entries": generated_entries,
        "coverage": ["lint", "deterministic uncompressed PBO" if backend == "builtin" else "реальный AddonBuilder", "свежий отдельный output", "PBO readback/prefix/entries", "полный inventory с явными generated исключениями", "SHA256 несжатых entries", "source drift"],
        "skipped": ["Binarize", "Enforce-компиляция", "игровое поведение", "deployment", "подпись"] + skipped_content,
    }
    (output / (expected.name + ".receipt.json")).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


def deploy(pbo, target, *, run=False, replace=False):
    pbo = checked_path(pbo, exists=True, directory=False)
    target = checked_path(target)
    registered = deployment_registered(target)
    if run and not registered:
        raise PipelineError("Deployment target не зарегистрирован в deployment_targets: " + str(target))
    inspected = inspect_pbo(pbo)
    destination = checked_path(target / pbo.name)
    if destination == pbo:
        raise PipelineError("Источник и установленный PBO совпадают")
    if pbo.is_relative_to(target):
        raise PipelineError("Deployment должен иметь отдельный каталог")
    stale = list(target.glob(pbo.name + ".*.bisign")) if target.exists() else []
    if stale:
        raise PipelineError("У целевого PBO есть подписи прежней сборки; сначала согласуй их замену вручную")
    if destination.exists() and not replace:
        raise PipelineError("Целевой PBO существует; для замены нужен --replace")
    result = {"status": "planned", "source": str(pbo), "target": str(destination), "sha256": inspected["sha256"],
              "target_registered": registered, "execution_blocked": not registered,
              "coverage": ["исходный PBO readback", "точный целевой файл", "проверка старых подписей"],
              "skipped": ["копирование", "игровой запуск", "подпись"]}
    if not run:
        return result
    target.mkdir(parents=True, exist_ok=True)
    temporary = target / (pbo.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        shutil.copyfile(pbo, temporary)
        if sha256(temporary) != inspected["sha256"] or sha256(pbo) != inspected["sha256"]:
            raise PipelineError("SHA256 изменился во время deployment")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    if sha256(destination) != inspected["sha256"]:
        raise PipelineError("SHA256 установленного PBO не совпадает")
    return {**result, "status": "passed", "coverage": result["coverage"] + ["atomic replace", "SHA256 назначения"],
            "skipped": ["игровой запуск", "подпись"], "previous_installed_pbo_backup": False}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--tools", default=DEFAULT_TOOLS)
    check.add_argument("--source")
    new = commands.add_parser("scaffold")
    new.add_argument("--workspace", required=True)
    new.add_argument("--name", required=True)
    new.add_argument("--side", choices=("client", "server"), default="client")
    lint_parser = commands.add_parser("lint")
    lint_parser.add_argument("--source", required=True)
    pack = commands.add_parser("build")
    pack.add_argument("--source", required=True)
    pack.add_argument("--output", required=True)
    pack.add_argument("--tools", default=DEFAULT_TOOLS)
    pack.add_argument("--temp")
    pack.add_argument("--timeout", type=int, default=180)
    pack.add_argument("--backend", choices=("builtin", "addonbuilder"), default="builtin")
    pack.add_argument("--run", action="store_true")
    inspect = commands.add_parser("inspect-pbo")
    inspect.add_argument("--pbo", required=True)
    copy = commands.add_parser("deploy")
    copy.add_argument("--pbo", required=True)
    copy.add_argument("--target", required=True)
    copy.add_argument("--run", action="store_true")
    copy.add_argument("--replace", action="store_true")
    return result


def main(argv=None):
    arguments = vars(parser().parse_args(argv))
    command = arguments.pop("command")
    try:
        result = {"preflight": preflight, "scaffold": scaffold, "lint": lint,
                  "build": build, "inspect-pbo": lambda pbo: inspect_pbo(pbo), "deploy": deploy}[command](**arguments)
    except (PipelineError, OSError, ValueError) as error:
        result = {"status": "failed", "error": str(error), "coverage": [], "skipped": ["операция остановлена"]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] in {"failed", "blocked"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
