"""Локальные снимки проверенной памяти без чтения переписки и вызовов API.

Использование: Python checkpoint_hook.py --root . --snapshot
Хуки Codex передают событие JSON через stdin. Вывод содержит только указатели.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path


FILES = ('AGENTS.md', '.ai-rules.md', 'memory/CURRENT.md', 'memory/PROFILE.md', 'memory/INDEX.md', 'memory/experience.json', 'tools/memory/routes.json', 'core/config.json', 'workbench/config.json', 'workbench/research_config.json', 'workbench/catalog.json', 'workbench/roles.json', 'workbench/START_HERE.md', 'memory/cards/dayzmods-architecture.md', 'memory/cards/dayzmods-gameplay.md', 'memory/cards/dayzmods-config.md', 'memory/cards/dayzmods-compatibility.md', 'memory/cards/dayzmods-build.md', 'memory/cards/dayzmods-ai-engineering.md', 'memory/cards/dayzmods-engine-coding.md', 'memory/cards/dayzmods-core.md', 'memory/cards/dayzmods-workbench.md')
FORCE = {"Stop", "PreCompact", "Interrupt", "SessionEnd", "Manual"}
EVENTS = FORCE | {"SessionStart", "PostToolUse"}
SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|-----BEGIN .*PRIVATE KEY-----|"
    r"(?:password|passwd|api_key|token|secret)\s*[:=]\s*[\"']?[^\s\"'<>]{8,})",
    re.IGNORECASE,
)


def checked_text(path):
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Недопустимый источник памяти")
    raw = path.read_bytes()
    if SECRET.search(raw.decode("utf-8-sig")):
        raise ValueError("Подозрение на секрет: сохранение остановлено")
    return raw


def atomic_write(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def process(root, event, now=None):
    now = time.time() if now is None else now
    name = event.get("hook_event_name", "")
    if name not in EVENTS:
        return {}
    # Корень задаётся установщиком, а не содержимым события или перепиской.
    root = root.resolve(strict=True)
    cwd = Path(event.get("cwd") or root).resolve()
    if not cwd.is_relative_to(root):
        return {}
    memory = root / "memory"
    store = memory / "checkpoints"
    for directory in (memory, store):
        directory.mkdir(exist_ok=True)
        if directory.is_symlink() or not directory.resolve().is_relative_to(root):
            raise ValueError("Недопустимый каталог памяти")
    # Одна блокировка для параллельных событий. Файл остаётся, блокировку снимает ОС.
    with (store / ".lock").open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return {}
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return update(root, store, name, now)
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def update(root, store, name, now):
    state_path = store / "state.json"
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {}
    calls = state.get("calls", 0) + (name == "PostToolUse")
    due = name in FORCE or calls >= 20 or now - state.get("checked", 0) >= 900
    if due:
        data = {}
        for relative in FILES:
            path = root / relative
            if not path.resolve().is_relative_to(root):
                raise ValueError("Источник вне проекта")
            data[relative] = checked_text(path)
        digest = hashlib.sha256()
        for relative, raw in data.items():
            digest.update(relative.encode() + b"\0" + raw + b"\0")
        signature = digest.hexdigest()
        if signature != state.get("signature"):
            # Только разрешённые файлы; tool_input/response/transcript не сохраняются.
            target = store / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + uuid.uuid4().hex[:8])
            target.mkdir()
            for relative, raw in data.items():
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
            manifest = {"event": name, "created_utc": now, "sha256": {
                relative: hashlib.sha256(raw).hexdigest() for relative, raw in data.items()
            }}
            (target / "manifest.json").write_text(json.dumps(manifest, indent=4), "utf-8")
            state["snapshot"] = target.name
            state["signature"] = signature
        state.update(checked=now, calls=0)
    else:
        state["calls"] = calls
    atomic_write(state_path, json.dumps(state, indent=4).encode())
    if name == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": name, "additionalContext":
            f"Проект Бобра: прочитай {root / '.ai-rules.md'} и {root / 'memory/CURRENT.md'}, "
            "если они ещё не загружены. Остальные источники — только по текущей задаче."}}
    if name == "PostToolUse" and due:
        return {"hookSpecificOutput": {"hookEventName": name, "additionalContext":
            "Контрольная точка: если есть новый результат или решение, обнови memory/CURRENT.md "
            "кратко (цель, сделано, проверено, следующий шаг). Снимок файлов сохранён; "
            "это не автоматическая сводка новых мыслей или переписки."}}
    return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--snapshot", action="store_true")
    args = parser.parse_args()
    try:
        event = {"hook_event_name": "Manual"} if args.snapshot else json.load(sys.stdin)
        print(json.dumps(process(args.root, event), ensure_ascii=True))
    except Exception:
        # Не печатать содержимое потенциально секретного файла или входного события.
        print(json.dumps({"systemMessage": "Бобр: checkpoint не сохранён; проверь локальный инструмент памяти."}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
