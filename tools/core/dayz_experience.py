"""Небольшое локальное хранилище явно записанного опыта без сбора данных, моделей и сети.

Вызывающий код передаёт проверенное резюме и фактический статус его подтверждения.
Ссылки на источники остаются строками: по ним ничего не открывается и не исполняется.
Выборка возвращает данные, а не инструкции или дополнительные разрешения.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid


class ExperienceError(Exception):
    """Базовый класс предусмотренных ошибок хранилища."""


class ExperienceValidationError(ExperienceError):
    """Неверные данные, небезопасный путь, повреждённая схема или превышение объёма."""


class ExperienceLockTimeout(ExperienceError):
    """Другой процесс записи удерживал блокировку дольше разрешённого ожидания."""


STATUSES = frozenset(("user_decision", "source_confirmed", "runtime_confirmed", "hypothesis"))
_FIELDS = frozenset(("id", "topic", "title", "body", "source", "source_hash", "status",
                     "created_at", "supersedes", "superseded_by"))
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_LENGTH = {"topic": 96, "title": 180, "body": 6000, "source": 1024}


def _text(value, name, maximum=None, multiline=False):
    maximum = maximum or _MAX_LENGTH[name]
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ExperienceValidationError(f"{name}: nonempty text of at most {maximum} characters required")
    permitted = "\n\t" if multiline else ""
    if any(ord(c) < 32 and c not in permitted for c in value) or "\x7f" in value:
        raise ExperienceValidationError(f"{name}: control characters are not allowed")
    return value.strip()


def _identifier(value, name):
    if not isinstance(value, str):
        raise ExperienceValidationError(f"{name}: canonical UUID required")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ExperienceValidationError(f"{name}: canonical UUID required") from exc
    if str(parsed) != value:
        raise ExperienceValidationError(f"{name}: canonical UUID required")
    return value


def _hash(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
        raise ExperienceValidationError("source_hash: lowercase SHA-256 hex or null required")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExperienceValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _not_constant(value):
    raise ExperienceValidationError(f"invalid JSON constant: {value}")


class FileMemory:
    """Атомарный снимок JSON с согласованной блокировкой записи между процессами.

    limit_bytes ограничивает весь файл UTF-8. budget метода recall ограничивает
    число выводимых символов, а не токенов. Создание объекта и чтение ничего не пишут.
    Пути с символьными ссылками, junction или ADS отклоняются. Перед операциями
    ввода-вывода пути проверяются повторно. Хранилище рассчитано на согласованную
    работу локальных процессов; оно не обеспечивает изоляцию ОС от постороннего
    процесса, который одновременно подменяет родительские каталоги.
    """

    LOCK_TIMEOUT_SECONDS = 2.0
    LOCK_POLL_SECONDS = 0.02
    MAX_RECALL_ITEMS = 8
    MAX_RECALL_CHARS = 12000

    def __init__(self, agent_root, limit_bytes=196608):
        if isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int) or not 128 <= limit_bytes <= 1048576:
            raise ExperienceValidationError("limit_bytes must be an integer in [128, 1048576]")
        raw = os.fspath(agent_root)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ExperienceValidationError("agent_root must be an absolute local directory")
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            raise ExperienceValidationError("agent_root must be absolute without '..'")
        # В Windows двоеточие разрешено только после буквы диска; прочие означают ADS.
        drive, tail = os.path.splitdrive(raw)
        if ":" in tail or (drive and (len(drive) != 2 or drive[1] != ":")) or raw.startswith(("\\\\", "//")):
            raise ExperienceValidationError("ADS, device, and network paths are not allowed")
        self.root = Path(os.path.abspath(raw))
        self.scope = str(self.root)
        self.limit_bytes = limit_bytes
        self.directory = self.root / "memory"
        self.path = self.directory / "experience.json"
        self.lock_path = self.directory / ".experience.lock"
        self._guard(self.root)
        if not self.root.is_dir():
            raise ExperienceValidationError("agent_root must be an existing directory")
        self._guard(self.path)

    def _guard(self, path):
        """Отклонить точки повторной обработки во всех существующих частях пути, включая корень."""
        for item in reversed((path, *path.parents)):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ExperienceValidationError(f"cannot inspect store path: {item}") from exc
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
                raise ExperienceValidationError(f"symlink/junction/reparse point refused: {item}")
            if item != path and not stat.S_ISDIR(info.st_mode):
                raise ExperienceValidationError(f"non-directory ancestor: {item}")
            if item == path and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ExperienceValidationError(f"hard-linked store file refused: {item}")

    def _empty(self):
        return {"schema_version": 1, "scope": self.scope, "records": []}

    def _validate_data(self, data):
        if not isinstance(data, dict) or set(data) != {"schema_version", "scope", "records"}:
            raise ExperienceValidationError("invalid experience root schema")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ExperienceValidationError("unsupported experience schema_version")
        if data["scope"] != self.scope or not isinstance(data["records"], list):
            raise ExperienceValidationError("experience scope mismatch or invalid records")
        by_id = {}
        for position, entry in enumerate(data["records"]):
            if not isinstance(entry, dict) or set(entry) != _FIELDS:
                raise ExperienceValidationError("invalid experience record schema")
            item_id = _identifier(entry["id"], "id")
            if item_id in by_id:
                raise ExperienceValidationError("duplicate experience id")
            for name in _MAX_LENGTH:
                canonical = _text(entry[name], name, multiline=name == "body")
                if canonical != entry[name]:
                    raise ExperienceValidationError(f"non-canonical {name}")
            if not isinstance(entry["status"], str) or entry["status"] not in STATUSES:
                raise ExperienceValidationError("invalid evidence status")
            _hash(entry["source_hash"])
            stamp = entry["created_at"]
            if not isinstance(stamp, str) or len(stamp) != 27 or not stamp.endswith("Z"):
                raise ExperienceValidationError("invalid created_at")
            try:
                datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ")
            except ValueError as exc:
                raise ExperienceValidationError("invalid created_at") from exc
            for name in ("supersedes", "superseded_by"):
                if entry[name] is not None:
                    _identifier(entry[name], name)
            by_id[item_id] = (position, entry)
        for position, entry in enumerate(data["records"]):
            old_id, new_id = entry["supersedes"], entry["superseded_by"]
            if old_id is not None:
                old = by_id.get(old_id)
                if not old or old[0] >= position or old[1]["superseded_by"] != entry["id"] or old[1]["topic"] != entry["topic"]:
                    raise ExperienceValidationError("invalid supersedes relation")
            if new_id is not None:
                new = by_id.get(new_id)
                if not new or new[0] <= position or new[1]["supersedes"] != entry["id"] or new[1]["topic"] != entry["topic"]:
                    raise ExperienceValidationError("invalid superseded_by relation")
        return data

    def _load(self):
        self._guard(self.path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return self._empty(), 0
        except OSError as exc:
            raise ExperienceValidationError("cannot open experience snapshot") from exc
        try:
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or getattr(info, "st_file_attributes", 0) & _REPARSE:
                    raise ExperienceValidationError("experience snapshot must be a plain file")
                if info.st_size > self.limit_bytes:
                    raise ExperienceValidationError("experience snapshot exceeds byte limit")
                raw = stream.read(self.limit_bytes + 1)
        except OSError as exc:
            raise ExperienceValidationError("cannot read experience snapshot") from exc
        if len(raw) > self.limit_bytes:
            raise ExperienceValidationError("experience snapshot exceeds byte limit")
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_not_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ExperienceValidationError("corrupt experience JSON; refusing overwrite") from exc
        return self._validate_data(data), len(raw)

    @contextmanager
    def _locked(self):
        self._guard(self.directory)
        try:
            self.directory.mkdir(exist_ok=True)
        except OSError as exc:
            raise ExperienceValidationError("cannot create memory directory") from exc
        self._guard(self.directory)
        deadline = time.monotonic() + self.LOCK_TIMEOUT_SECONDS
        token = f"{os.getpid()}:{uuid.uuid4()}".encode("ascii")
        acquired = False
        identity = None
        try:
            while True:
                self._guard(self.lock_path)
                try:
                    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(self.lock_path, flags, 0o600)
                    acquired = True
                    with os.fdopen(descriptor, "wb") as stream:
                        info = os.fstat(stream.fileno())
                        identity = (info.st_dev, info.st_ino)
                        stream.write(token)
                        stream.flush()
                        os.fsync(stream.fileno())
                    break
                except FileExistsError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ExperienceLockTimeout("experience writer lock timed out; no stale lock was removed")
                    time.sleep(min(self.LOCK_POLL_SECONDS, remaining))
                except OSError as exc:
                    raise ExperienceError("cannot acquire experience writer lock") from exc
            self._guard(self.lock_path)
            yield
        finally:
            if acquired:
                self._guard(self.lock_path)
                try:
                    info = self.lock_path.stat()
                    if identity == (info.st_dev, info.st_ino):
                        self.lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _save(self, data):
        self._validate_data(data)
        raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(raw) > self.limit_bytes:
            raise ExperienceValidationError("experience byte budget exhausted; nothing was overwritten")
        self._guard(self.path)
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".experience.", suffix=".tmp", dir=self.directory)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._guard(self.path)
            self._guard(temporary)
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            raise ExperienceError("atomic experience write failed; prior snapshot preserved") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def record(self, topic, title, body, source, source_hash=None, status="source_confirmed", supersedes=None):
        """Добавить проверенное резюме; прежняя запись меняется только при явном supersedes."""
        values = {"topic": topic, "title": title, "body": body, "source": source}
        values = {name: _text(value, name, multiline=name == "body") for name, value in values.items()}
        if not isinstance(status, str) or status not in STATUSES:
            raise ExperienceValidationError("invalid evidence status; never inferred from source")
        _hash(source_hash)
        if supersedes is not None:
            _identifier(supersedes, "supersedes")
        with self._locked():
            data, _ = self._load()
            previous = None
            if supersedes is not None:
                previous = next((entry for entry in data["records"] if entry["id"] == supersedes), None)
                if previous is None or previous["superseded_by"] is not None or previous["topic"] != values["topic"]:
                    raise ExperienceValidationError("supersedes must name an active record in the same topic and scope")
            item_id = str(uuid.uuid4())
            if previous is not None:
                previous["superseded_by"] = item_id
            entry = dict(id=item_id, **values, source_hash=source_hash, status=status,
                         created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                         supersedes=supersedes, superseded_by=None)
            data["records"].append(entry)
            self._save(data)
        return {"id": item_id, "scope": self.scope, "topic": values["topic"], "status": status,
                "supersedes": supersedes, "superseded_ids": [supersedes] if supersedes else []}

    def validate(self):
        """Проверить схему, связи и объём в байтах; отсутствие снимка означает пустое хранилище."""
        data, size = self._load()
        inactive = sum(entry["superseded_by"] is not None for entry in data["records"])
        return {"valid": True, "scope": self.scope, "path": str(self.path), "records": len(data["records"]),
                "active": len(data["records"]) - inactive, "superseded": inactive, "bytes": size}

    def recall(self, topic, query=None, budget=1500):
        """Выбрать действующие сведения в пределах budget, заданного в выводимых символах."""
        topic = _text(topic, "topic")
        if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= self.MAX_RECALL_CHARS:
            raise ExperienceValidationError(f"budget must be integer characters in [0, {self.MAX_RECALL_CHARS}]")
        if query is not None:
            query = _text(query, "query", maximum=512)
        data, _ = self._load()
        terms = set(re.findall(r"\w+", query.casefold())) if query else set()
        candidates = []
        for position, entry in enumerate(data["records"]):
            if entry["superseded_by"] is not None or entry["topic"].casefold() != topic.casefold():
                continue
            title, body = entry["title"].casefold(), entry["body"].casefold()
            score = sum(3 * (term in title) + (term in body) for term in terms)
            if terms and score == 0:
                continue
            candidates.append((score, position, entry))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        fragments, items, consumed = [], [], 0
        for _, _, entry in candidates[:self.MAX_RECALL_ITEMS]:
            label = "ГИПОТЕЗА — не подтверждено" if entry["status"] == "hypothesis" else entry["status"]
            header = f"[{label}] {entry['title']}\nid: {entry['id']}\nsource: {entry['source']}\n"
            separator = "\n\n" if fragments else ""
            available = budget - consumed - len(separator) - len(header)
            if available < 1:
                continue
            body = entry["body"]
            excerpt = body[:available]
            if len(body) > available:
                matches = [body.casefold().find(term) for term in terms if term in body.casefold()]
                first = min(matches) if matches else 0
                start = max(0, first - 48) if first >= available else 0
                prefix = "…" if start else ""
                width = max(0, available - len(prefix) - 1)
                excerpt = (prefix + body[start:start + width] + "…")[:available]
            fragment = separator + header + excerpt
            fragments.append(fragment)
            consumed += len(fragment)
            items.append({"id": entry["id"], "title": entry["title"], "status": entry["status"],
                          "source": entry["source"], "source_hash": entry["source_hash"], "excerpt": excerpt})
        truncated = len(items) < len(candidates) or any(len(item["excerpt"]) < len(next(e for e in data["records"] if e["id"] == item["id"])["body"]) for item in items)
        return {"scope": self.scope, "topic": topic, "query": query, "budget_chars": budget,
                "trust": "data_only", "text": "".join(fragments), "items": items, "truncated": truncated,
                "warnings": ["hypothesis_is_not_confirmed"] if any(item["status"] == "hypothesis" for item in items) else []}
