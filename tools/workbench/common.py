"""Общие файлы и результаты DayZ Workbench; без сети и игровых процессов."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone


AGENT = Path(__file__).resolve().parents[2]
VERSION = "2.0.1"
MAX_TEXT = 8 * 1024 * 1024
PRIVATE_PARTS = {"secrets", "keys", ".git", ".ssh", "storage_1", "players.db"}


def now():
    return datetime.now(timezone.utc).isoformat()


def checked_path(value, *, exists=False):
    path = Path(value).absolute()
    if ".." in path.parts or str(path).startswith("\\\\"):
        raise ValueError("Требуется локальный путь без обратных переходов")
    for part in path.parts:
        if part.lower() in PRIVATE_PARTS or part.lower().startswith(".env") or part.lower().endswith(".biprivatekey"):
            raise ValueError("Приватный путь не входит в рабочий пакет")
    for current in (path, *path.parents):
        if current.exists() or current.is_symlink():
            attributes = current.lstat()
            if current.is_symlink() or getattr(attributes, "st_file_attributes", 0) & 0x400:
                raise ValueError("Symlink/junction требует отдельного явно проверенного адаптера")
    if exists and not path.exists():
        raise ValueError("Не найден путь: " + str(path))
    return path


def text_file(path, limit=MAX_TEXT):
    return source_bytes(path, limit).decode("utf-8-sig")


def source_bytes(path, limit=MAX_TEXT):
    path = checked_path(path, exists=True)
    if path.stat().st_size > limit:
        raise ValueError("Файл превышает лимит адресного чтения: " + str(path))
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Файл вырос во время чтения")
    return data


def read_json(path):
    return json.loads(text_file(path))


def atomic_json(path, value):
    path = checked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=4) + "\n").encode("utf-8")
    if len(payload) > MAX_TEXT:
        raise ValueError("JSON превышает рабочий лимит")
    descriptor, temporary = tempfile.mkstemp(prefix=".bobr-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha(path):
    path = checked_path(path, exists=True)
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()


def tree(root, suffixes=None, limit=30000):
    root = checked_path(root, exists=True)
    if not root.is_dir():
        raise ValueError("Нужен каталог")
    output = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in names:
            checked_path(Path(directory) / name)
        for name in sorted(filenames):
            path = checked_path(Path(directory) / name)
            if suffixes is None or path.suffix.lower() in suffixes:
                output.append(path)
            if len(output) > limit:
                raise ValueError("Слишком большой набор; сузь область проверки")
    return sorted(output)


def result(status, coverage, *, skipped=(), findings=(), **extra):
    return {"status": status, "coverage": list(coverage), "skipped": list(skipped),
            "findings": list(findings), **extra}


def redact(value):
    value = re.sub(r'''(?i)((?:["']?)(?:password|passwd|token|api[_-]?key|webhook|secret)(?:["']?)\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;<>]+)''', r"\1[REDACTED]", value)
    value = re.sub(r"(?is)(<(?:password|passwd|token|api[_-]?key|webhook|secret)>).*?(</(?:password|passwd|token|api[_-]?key|webhook|secret)>)", r"\1[REDACTED]\2", value)
    value = re.sub(r"\b\d{17}\b", "[PLAYER_ID]", value)
    value = re.sub(r"https://(?:discord(?:app)?\.com/api/webhooks|[^\s]+[?&](?:token|key)=)[^\s]+", "[PRIVATE_URL]", value)
    def address(match):
        candidate = match.group(0)
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return candidate
        return "[IP_ADDRESS]"
    value = re.sub(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", address, value)
    value = re.sub(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:.]*(?:%[\w.-]+)?(?![\w:])", address, value)
    return value


def load_config():
    path = checked_path(os.environ.get("BOBR_WORKBENCH_CONFIG", AGENT / "workbench/config.json"))
    if not path.is_file():
        raise FileNotFoundError("Конфигурация отсутствует; выполни python configure.py: " + str(path))
    return read_json(path)
