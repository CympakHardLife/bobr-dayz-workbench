"""Настроить переносимый DayZ Workbench без скачивания и установки служб.

    <absolute-existing-python> configure.py

Пути вычисляются из расположения файла и явных параметров владельца.
Повторная настройка сохраняет память. Знания и модель подключаются явно.
Внутри репозитория данные размещаются только в .local; другие data-root — внешние.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


VERSION = "2.0.3"
ROOT = Path(__file__).resolve().parent
SCOPES = {
    "dayz": ("DayZ", ["BohemiaInteractive__DayZ-Script-Diff"]),
    "dayz-experimental": ("DayZ experimental", ["BohemiaInteractive__DayZ-Script-Diff-Experimental"]),
    "reforger": ("Arma Reforger", ["BohemiaInteractive__Arma-Reforger-Script-Diff"]),
    "toolkits": ("Optional toolkit references", ["DayZ-n-Chill__Agentic-Z", "StarDZ-Team__Dayz-Modding-Skills"]),
    "plugmem": ("Optional memory research", ["TIMAN-group__PlugMem"]),
}
SOURCES = ["https://community.bistudio.com/wiki", "https://github.com/BohemiaInteractive/DayZ-Script-Diff",
           "https://github.com/ExpansionModTeam/DayZ-Expansion-Scripts", "https://github.com/ysaroka/InediaInfectedAI",
           "https://raw.githubusercontent.com/BohemiaInteractive/DayZ-Script-Diff",
           "https://raw.githubusercontent.com/ExpansionModTeam/DayZ-Expansion-Scripts",
           "https://raw.githubusercontent.com/ysaroka/InediaInfectedAI", "https://github.com/ggml-org/llama.cpp",
           "https://huggingface.co/Qwen", "https://qwen.readthedocs.io/en/latest", "https://learn.chatgpt.com/docs"]


def checked_path(value, *, file=False):
    path = Path(value).expanduser().absolute()
    if ".." in path.parts or str(path).startswith("\\\\"):
        raise ValueError("Нужен локальный путь без обратных переходов")
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Symlink/junction требует отдельной проверки")
    if any(part.casefold() in {"secrets", "keys", ".git", ".ssh"} for part in path.parts):
        raise ValueError("Приватная папка не используется настройщиком")
    if file and not path.is_file():
        raise ValueError("Нет указанного файла: " + str(path))
    return path.resolve(strict=file)


def digest_file(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path, default=None):
    if not path.is_file():
        return default
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("JSON превысил лимит настройки")
    with path.open("rb") as stream:
        return json.loads(stream.read(2 * 1024 * 1024 + 1).decode("utf-8-sig"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def pinned_file(value, expected, label):
    path = checked_path(value, file=True)
    actual = digest_file(path)
    if expected and actual.lower() != expected.lower():
        raise ValueError("SHA не совпал: " + label)
    return str(path), actual


def source_manifest(root):
    # Явный экспортный allowlist не обходит data-root и пользовательские файлы.
    exported = read_json(root / "SOURCE_MANIFEST.json")
    if not isinstance(exported, dict) or not isinstance(exported.get("files"), dict):
        raise ValueError("Нет корректного SOURCE_MANIFEST.json")
    names = set(exported["files"]) | {"SOURCE_MANIFEST.json", "core/config.json", "workbench/config.json", "workbench/research_config.json"}
    manifest = {}
    for relative in sorted(names):
        item = Path(relative)
        if not relative or item.is_absolute() or ".." in item.parts or "\\" in relative or ":" in relative:
            raise ValueError("Небезопасный путь source manifest")
        if relative.startswith("memory/"):
            continue
        path = checked_path(root / item, file=True)
        if not path.is_relative_to(root):
            raise ValueError("Source manifest выходит из репозитория")
        manifest[relative] = digest_file(path)
    required = {"tools/core/dayz_core.py", "tools/core/dayz_experience.py", "tools/core/engine_library_index.py",
                "core/config.json", ".agents/skills/dayz-modding-core/SKILL.md"}
    if not required.issubset(manifest):
        raise ValueError("Экспорт неполон: отсутствуют обязательные файлы Core")
    return {"version": VERSION, "scope": "own_source_and_generated_configuration",
            "mutable_memory_excluded": True, "files": manifest}


def configure(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("data-root", "tools-root", "knowledge-root", "model", "llama", "model-sha256", "llama-sha256", "runtime-manifest", "owner"):
        parser.add_argument("--" + option)
    parser.add_argument("--assistant-name", default="Бобр")
    parser.add_argument("--disable-model", action="store_true")
    parser.add_argument("--no-snapshot", action="store_true")
    args = parser.parse_args(argv)
    root = checked_path(root or ROOT)
    if not (root / "tools/workbench/cli.py").is_file():
        raise ValueError("Запусти configure.py из полного переносимого пакета")
    old = read_json(root / "workbench/config.json", {})
    data = checked_path(args.data_root or old.get("data_root") or root / ".local")
    tools = checked_path(args.tools_root or old.get("tools_root") or data / "unconfigured/dayz-tools")
    knowledge = checked_path(args.knowledge_root or old.get("knowledge_root") or data / "unconfigured/knowledge")
    if data == root or root.is_relative_to(data):
        raise ValueError("data-root должен быть отдельной папкой данных, не корнем/родителем исходников")
    if data.is_relative_to(root) and not data.is_relative_to(root / ".local"):
        raise ValueError("Внутри репозитория data-root разрешён только в .local; выбери .local либо внешний абсолютный путь")
    if args.owner is not None and (not args.owner.strip() or len(args.owner) > 120 or any(ord(char) < 32 for char in args.owner)):
        raise ValueError("Имя владельца: 1–120 символов без управляющих знаков")
    if not args.assistant_name.strip() or len(args.assistant_name) > 120 or any(ord(char) < 32 for char in args.assistant_name):
        raise ValueError("Имя помощника: 1–120 символов без управляющих знаков")
    if bool(args.model) != bool(args.llama) or (args.disable_model and args.model):
        raise ValueError("Выбери --disable-model либо одновременно --model и --llama")
    experience_path = root / "memory/experience.json"
    experience_template = read_json(root / "templates/memory/experience.json")
    if not isinstance(experience_template, dict):
        raise ValueError("Нет шаблона memory/experience.json")
    profile_template = (root / "templates/memory/PROFILE.md").read_text("utf-8-sig")
    current_template = (root / "templates/memory/CURRENT.md").read_text("utf-8-sig")
    experience = read_json(experience_path, experience_template)
    if experience.get("scope") not in {"", str(root)} and experience.get("records"):
        raise ValueError("Непустая память принадлежит другому корню; требуется явная миграция")
    experience["scope"] = str(root)
    research_path = root / "workbench/research_config.json"
    previous_research = read_json(research_path, {})
    model = previous_research.get("model", {}) if not args.disable_model else {}
    if args.model:
        model_path, model_sha = pinned_file(args.model, args.model_sha256, "model")
        binary_path, binary_sha = pinned_file(args.llama, args.llama_sha256, "llama")
        if Path(model_path).stat().st_size > 1500000000:
            raise ValueError("Модель превышает лимит 1.5GB")
        model = {"name": Path(model_path).stem, "file": model_path, "sha256": model_sha,
                 "binary": binary_path, "binary_sha256": binary_sha, "source": "owner_supplied_local_files"}
        if args.runtime_manifest:
            model["runtime_manifest"] = str(checked_path(args.runtime_manifest, file=True))
    workbench = {"schema_version": 2, "version": VERSION, "agent_root": str(root), "data_root": str(data),
                 "workspace_root": str(data / "projects"), "test_workspace": str(data / "tests"),
                 "runs_root": str(data / "runs"), "reports_root": str(data / "reports"),
                 "builds_root": str(data / "builds"), "tools_root": str(tools),
                 "knowledge_root": str(knowledge), "temp_root": str(data / "temp"),
                 "workdrive": str(data / "unconfigured/workdrive"), "game_root": str(data / "unconfigured/dayz"),
                 "server_root": str(data / "unconfigured/dayz-server"), "automatic_game_launch": False,
                 "signing": "owner_manually", "network": False, "approved_write_roots": [str(data)],
                 "deployment_targets": [], "native_agents": ".codex/agents", "skills": ".agents/skills"}
    core = {"schema_version": 1, "agent_root": str(root), "storage_drive": data.drive or data.anchor,
            "python_executable": sys.executable, "cache": {"root": str(data / "cache/core"), "ttl_seconds": 86400,
            "max_entries": 128, "max_payload_bytes": 65536},
            "scopes": {name: {"root": str(knowledge), "game": game, "datasets": datasets} for name, (game, datasets) in SCOPES.items()},
            "backends": {"local": {"status": "active", "implementation": "File memory and optional SQLite FTS5 corpora", "network": False},
                         **{name: {"status": "disabled", "adapter_implemented": False} for name in ("pinecone", "chroma", "milvus", "plugmem", "mem0", "letta")}}}
    research = {"schema_version": 1, "core_config": str(root / "core/config.json"),
                "cache_root": str(data / "cache/research"), "timeout_seconds": 15, "cache_ttl_seconds": 86400,
                "max_cache_entries": 64, "model_timeout_seconds": 90, "model": model,
                "allowed_cache_roots": [str(data)], "allowed_sources": SOURCES}
    for directory in (data, data / "projects", data / "tests", data / "runs", data / "reports", data / "builds", data / "temp", data / "cache"):
        checked_path(directory).mkdir(parents=True, exist_ok=True)
    write_json(root / "core/config.json", core)
    write_json(root / "workbench/config.json", workbench)
    write_json(research_path, research)
    write_json(experience_path, experience)
    for name, template in (("PROFILE.md", profile_template), ("CURRENT.md", current_template)):
        destination = root / "memory" / name
        if not destination.exists():
            destination.write_text(template, encoding="utf-8")
    if args.owner is not None:
        (root / "memory/PROFILE.md").write_text("# Профиль владельца\n\nВладелец: " + args.owner.strip()
            + "\nПомощник: " + args.assistant_name.strip() + "\n\nПредпочтения дополняются только явными договорённостями.\n", encoding="utf-8")
    write_json(root / "core/CORE_MANIFEST.json", source_manifest(root))
    snapshot = {"status": "not_requested"}
    if not args.no_snapshot:
        process = subprocess.run([sys.executable, "-B", "-X", "utf8", str(root / "tools/memory/checkpoint_hook.py"),
                                  "--root", str(root), "--snapshot"], capture_output=True, text=True, encoding="utf-8", timeout=30)
        if process.returncode:
            raise ValueError("Настройка записана, но snapshot не создан; проверь память")
        snapshot = {"status": "created", "output": process.stdout.strip()}
    return {"status": "configured", "version": VERSION, "root": str(root), "data_root": str(data), "python": sys.executable,
            "model": "configured_local" if model else "disabled", "knowledge": "indexed_path_supplied" if args.knowledge_root else "optional_not_bundled",
            "snapshot": snapshot, "network_used": False, "services_installed": False}


def main(argv=None):
    try:
        print(json.dumps(configure(argv), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
