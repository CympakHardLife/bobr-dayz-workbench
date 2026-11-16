#!/usr/bin/env python3
"""Локальный индекс официальных документов и исходников, только stdlib.

    python engine_library_index.py index --root ./knowledge
    python engine_library_index.py search --root ./knowledge --dataset rv3 --query FSM
    python engine_library_index.py verify --root ./knowledge

index пишет только index.sqlite и index_manifest.json внутри root.
search/query и verify открывают SQLite только для чтения. Индекс не исполняет
код и не распаковывает архивы. symbol_count — лексическая оценка, не AST.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import quote, urlparse


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 16 * 1024 * 1024
TEXT_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".inc", ".sqf", ".sqs",
    ".cfg", ".ext", ".json", ".xml", ".md", ".txt", ".html", ".htm",
    ".csv", ".ini", ".py", ".rs", ".go", ".js", ".ts", ".tsx", ".css",
    ".yml", ".yaml", ".toml", ".fsm", ".bifsm", ".layout", ".rvmat",
    ".ps1", ".bat", ".cmd", ".sh", ".cmake", ".enf", ".gproj", ".conf",
    ".config", ".et", ".ct", ".rst", ".java", ".kt", ".gradle", ".tsv",
    ".sln", ".csproj", ".cs", ".mod", ".sum", ".g4", ".ebnf", ".grammar", ".pest", ".lst", ".mjs", ".cjs", ".mdx",
})
TEXT_NAMES = frozenset({"license", "copying", "readme", "authors", "notice", "changelog"})
SYMBOL_PATTERN = re.compile(
    r"\b(?:class|struct|interface|enum)\s+[A-Za-z_]\w*"
    r"|\b(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*\s*\([^;{}]*\)\s*(?:const\s*)?\{",
    re.MULTILINE,
)


def digest_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def check_plain(path: Path, root: Path) -> None:
    """Запрещает выход за root и symlink/junction в затрагиваемом дереве."""
    path = path.absolute()
    relative = path.relative_to(root)
    if ".." in relative.parts:
        raise ValueError(f"Обратный переход в пути запрещён: {path}")
    current = path
    while True:
        if current.exists() or current.is_symlink():
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
            if current.is_symlink() or attributes & 0x400:
                raise ValueError(f"Ссылка/reparse point запрещены: {current}")
        if current == root:
            return
        current = current.parent


def library_root(value: str | Path) -> Path:
    root = Path(value).absolute()
    if not root.is_dir():
        raise ValueError(f"Нет каталога базы: {root}")
    check_plain(root, root)
    if root.resolve() != root:
        raise ValueError("Путь root должен быть каноническим, без '..' или ссылок")
    return root


def source_files(root: Path):
    sources = root / "sources"
    check_plain(sources, root)
    if not sources.is_dir():
        raise ValueError(f"Нет каталога sources: {sources}")
    for dataset in sorted(sources.iterdir()):
        check_plain(dataset, root)
        if not dataset.is_dir():
            continue
        for current, directories, filenames in os.walk(dataset, followlinks=False):
            current_path = Path(current)
            for name in sorted(directories):
                check_plain(current_path / name, root)
            directories.sort()
            for name in sorted(filenames):
                path = current_path / name
                check_plain(path, root)
                if path.is_file():
                    yield dataset.name, path, path.relative_to(dataset).as_posix()


def record_for(root: Path, dataset: str) -> tuple[dict, Path]:
    path = root / "records" / f"{dataset}.json"
    check_plain(path, root)
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError(f"Нет допустимого records/{dataset}.json")
    record = json.loads(path.read_text("utf-8-sig"))
    if not isinstance(record, dict):
        raise ValueError(f"Record должен быть объектом: {path}")
    if record.get("dataset", dataset) != dataset:
        raise ValueError(f"Несовпадение dataset в {path}")
    return record, path


def is_text_path(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS or path.name.lower() in TEXT_NAMES


def decode_source(data: bytes, preferred: str | None) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    if b"\0" in data:
        raise ValueError("binary_nul")
    encodings = [preferred, "utf-8-sig", "utf-8", "cp1252"]
    for encoding in dict.fromkeys(item for item in encodings if item):
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        controls = sum(ord(char) < 32 and char not in "\r\n\t\f" for char in text)
        if controls > max(2, len(text) // 100):
            raise ValueError("binary_controls")
        return text, encoding
    raise ValueError("unsupported_encoding")


def file_source_url(record: dict, relative: str, entry: dict) -> str | None:
    if entry.get("source_url"):
        return entry["source_url"]
    for page in record.get("pages") or []:
        if relative in (page.get("files") or []):
            return page.get("source_url")
    url = str(record.get("source_url") or "")
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    commit = str(record.get("commit") or "")
    if parsed.netloc.lower() == "github.com" and len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        repository = "/".join(parts).removesuffix(".git")
        return f"https://github.com/{repository}/blob/{commit}/{quote(relative, safe='/')}"
    return None


def lexical_symbol_count(text: str) -> int:
    """Только число похожих на объявления конструкций; возможны пропуски/FP."""
    without_comments = re.sub(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\])*\"", " ", text, flags=re.DOTALL)
    return len(SYMBOL_PATTERN.findall(without_comments))


def build_index(root: Path, force_like: bool = False) -> dict:
    db_path = root / "index.sqlite"
    manifest_path = root / "index_manifest.json"
    for path in (db_path, manifest_path):
        check_plain(path, root)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=MEMORY")
    connection.execute("PRAGMA temp_store=MEMORY")
    records = {}
    skipped = []
    total = Counter()
    try:
        connection.executescript("""
            DROP TABLE IF EXISTS file_fts;
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS datasets;
            CREATE TABLE datasets(dataset TEXT PRIMARY KEY, metadata_json TEXT NOT NULL, record_sha256 TEXT NOT NULL);
            CREATE TABLE files(
                id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, encoding TEXT NOT NULL,
                line_count INTEGER NOT NULL, symbol_count INTEGER NOT NULL,
                source_url TEXT, file_source_url TEXT, content TEXT NOT NULL,
                UNIQUE(dataset,relative_path)
            );
            CREATE INDEX files_dataset ON files(dataset);
        """)
        backend = "like"
        if not force_like:
            try:
                connection.execute("CREATE VIRTUAL TABLE file_fts USING fts5(content, relative_path, content='files', content_rowid='id', tokenize='unicode61')")
                backend = "fts5"
            except sqlite3.OperationalError as error:
                if "fts5" not in str(error).lower():
                    raise
        for dataset, path, relative in source_files(root):
            if dataset not in records:
                record, record_path = record_for(root, dataset)
                entries = record.get("files") or []
                if not isinstance(entries, list):
                    raise ValueError(f"files не список: {dataset}")
                file_metadata = {str(item["path"]).replace("\\", "/"): item for item in entries if isinstance(item, dict) and "path" in item}
                record_sha = digest_file(record_path)
                records[dataset] = {"metadata": record, "record_sha256": record_sha, "entries": file_metadata, "indexed_files": 0}
                connection.execute("INSERT INTO datasets VALUES(?,?,?)", (dataset, json.dumps(record, ensure_ascii=False), record_sha))
            total["discovered_files"] += 1
            entry = records[dataset]["entries"].get(relative, {})
            size = path.stat().st_size
            if not is_text_path(path) or size > MAX_FILE_BYTES:
                skipped.append({"dataset": dataset, "path": relative, "reason": "extension" if not is_text_path(path) else "size_limit"})
                continue
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if entry.get("sha256") and str(entry["sha256"]).lower() != sha:
                raise ValueError(f"SHA не совпадает с record: {dataset}/{relative}")
            if "bytes" in entry and entry["bytes"] != len(data):
                raise ValueError(f"Размер не совпадает с record: {dataset}/{relative}")
            try:
                content, encoding = decode_source(data, entry.get("encoding"))
            except ValueError as error:
                skipped.append({"dataset": dataset, "path": relative, "reason": str(error)})
                continue
            if content.startswith("version https://git-lfs.github.com/spec/v1"):
                skipped.append({"dataset": dataset, "path": relative, "reason": "lfs_pointer"})
                continue
            metadata = records[dataset]["metadata"]
            symbols = lexical_symbol_count(content)
            cursor = connection.execute(
                "INSERT INTO files(dataset,relative_path,sha256,bytes,encoding,line_count,symbol_count,source_url,file_source_url,content) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (dataset, relative, sha, len(data), encoding, len(content.splitlines()), symbols, metadata.get("source_url"), file_source_url(metadata, relative, entry), content),
            )
            if backend == "fts5":
                connection.execute("INSERT INTO file_fts(rowid,content,relative_path) VALUES(?,?,?)", (cursor.lastrowid, content, relative))
            records[dataset]["indexed_files"] += 1
            total.update(indexed_files=1, indexed_bytes=len(data), lexical_symbol_candidates=symbols)
        connection.commit()
        connection.execute("PRAGMA optimize")
    finally:
        connection.close()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "index_sha256": digest_file(db_path),
        "counts": dict(total),
        "max_file_bytes": MAX_FILE_BYTES,
        "datasets": [{"dataset": name, "record_sha256": item["record_sha256"], "indexed_files": item["indexed_files"]} for name, item in sorted(records.items())],
        "skipped": skipped,
        "limitations": ["Лексический symbol_count не является AST/проверкой языка.", "API разных engine/game/dataset не объединяются в доказательство совместимости.", "Индекс не исполняет исходники и не подтверждает работу в игре."],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return {key: value for key, value in manifest.items() if key != "skipped"} | {"skipped_files": len(skipped)}


def readonly_index(root: Path) -> sqlite3.Connection:
    path = root / "index.sqlite"
    check_plain(path, root)
    if not path.is_file():
        raise ValueError("Индекс отсутствует; сначала выполни index")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    connection.create_function("casefold", 1, lambda value: (value or "").casefold(), deterministic=True)
    return connection


def load_manifest(root: Path) -> dict:
    path = root / "index_manifest.json"
    check_plain(path, root)
    manifest = json.loads(path.read_text("utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Неподдерживаемая версия manifest")
    return manifest


def snippets(content: str, query: str, context: int) -> list[dict]:
    lines = content.splitlines() or [""]
    words = re.findall(r"\w+", query.casefold())
    matches = [i for i, line in enumerate(lines) if any(word in line.casefold() for word in words)]
    result = []
    previous_end = -1
    for matched in matches or [0]:
        start, end = max(0, matched - context), min(len(lines), matched + context + 1)
        if start <= previous_end:
            continue
        result.append({"line": matched + 1, "snippet_start_line": start + 1, "snippet_end_line": end, "text": "\n".join(lines[start:end])[:1800]})
        previous_end = end - 1
        if len(result) == 3:
            break
    return result


def search_index(root: Path, query: str, dataset: str | None, limit: int, context: int) -> dict:
    if not query.strip() or len(query) > 512:
        raise ValueError("query должен содержать 1–512 символов")
    manifest = load_manifest(root)
    connection = readonly_index(root)
    try:
        parameters = []
        words = re.findall(r"\w+", query)
        if manifest["backend"] == "fts5" and words:
            match = " AND ".join('"' + word.replace('"', '""') + '"' for word in words)
            sql = "SELECT f.*,d.metadata_json FROM file_fts JOIN files f ON f.id=file_fts.rowid JOIN datasets d ON d.dataset=f.dataset WHERE file_fts MATCH ?"
            parameters.append(match)
            ordering = " ORDER BY bm25(file_fts),f.dataset,f.relative_path LIMIT ?"
        else:
            sql = "SELECT f.*,d.metadata_json FROM files f JOIN datasets d ON d.dataset=f.dataset WHERE instr(casefold(f.content || ' ' || f.relative_path),?) > 0"
            parameters.append(query.casefold())
            ordering = " ORDER BY f.dataset,f.relative_path LIMIT ?"
        if dataset is not None:
            sql += " AND f.dataset=?"
            parameters.append(dataset)
        parameters.append(limit)
        results = []
        for row in connection.execute(sql + ordering, parameters):
            metadata = json.loads(row["metadata_json"])
            result = {key: row[key] for key in ("dataset", "relative_path", "sha256", "bytes", "line_count", "symbol_count", "source_url", "file_source_url")}
            if not result["file_source_url"]:
                result["file_source_url"] = file_source_url(metadata, row["relative_path"], {})
            result.update({key: metadata.get(key) for key in ("kind", "engine", "game", "channel", "commit", "publisher", "license_api", "license_paths")})
            result["snippets"] = snippets(row["content"], query, context)
            results.append(result)
        return {"backend": manifest["backend"], "query": query, "dataset": dataset, "count": len(results), "results": results}
    finally:
        connection.close()


def verify_index(root: Path) -> dict:
    manifest = load_manifest(root)
    mismatches = []
    check_plain(root / "index.sqlite", root)
    if digest_file(root / "index.sqlite") != manifest["index_sha256"]:
        mismatches.append({"kind": "index_sha256"})
    connection = readonly_index(root)
    checked = 0
    expected_paths = {(entry["dataset"], entry["path"]) for entry in manifest.get("skipped", [])}
    try:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            mismatches.append({"kind": "sqlite_quick_check", "detail": integrity})
        for item in manifest["datasets"]:
            record, path = record_for(root, item["dataset"])
            if digest_file(path) != item["record_sha256"]:
                mismatches.append({"kind": "record_sha256", "dataset": item["dataset"]})
        for row in connection.execute("SELECT dataset,relative_path,sha256,bytes FROM files"):
            expected_paths.add((row["dataset"], row["relative_path"]))
            path = root / "sources" / row["dataset"] / row["relative_path"]
            check_plain(path, root)
            checked += 1
            if not path.is_file():
                mismatches.append({"kind": "missing_file", "dataset": row["dataset"], "path": row["relative_path"]})
            elif path.stat().st_size != row["bytes"] or digest_file(path) != row["sha256"]:
                mismatches.append({"kind": "file_drift", "dataset": row["dataset"], "path": row["relative_path"]})
        actual_paths = {(dataset, relative) for dataset, path, relative in source_files(root)}
        for dataset, relative in sorted(actual_paths - expected_paths):
            mismatches.append({"kind": "new_file", "dataset": dataset, "path": relative})
        for dataset, relative in sorted(expected_paths - actual_paths):
            if not any(item.get("kind") == "missing_file" and item.get("dataset") == dataset and item.get("path") == relative for item in mismatches):
                mismatches.append({"kind": "missing_skipped_file", "dataset": dataset, "path": relative})
        return {"ok": not mismatches, "checked_indexed_files": checked, "mismatches": mismatches}
    finally:
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    index = subparsers.add_parser("index")
    index.add_argument("--root", required=True)
    index.add_argument("--no-fts", action="store_true", help="Принудительный fallback для проверки")
    search = subparsers.add_parser("search", aliases=["query"])
    search.add_argument("--root", required=True)
    search.add_argument("--dataset")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--context", type=int, default=1)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    try:
        root = library_root(args.root)
        if args.command == "index":
            result = build_index(root, args.no_fts)
        elif args.command in ("search", "query"):
            if not 1 <= args.limit <= 200 or not 0 <= args.context <= 3:
                raise ValueError("limit: 1–200; context: 0–3")
            result = search_index(root, args.query, args.dataset, args.limit, args.context)
        else:
            result = verify_index(root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "verify" and not result["ok"] else 0
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
