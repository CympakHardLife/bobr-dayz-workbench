#!/usr/bin/env python3
"""Read-only проверка совпадений имён переменных с доступными типами Enforce.

Пример:
    python enforce_name_collisions.py --type-db symbols.sqlite \
        --types-from FULL_SOURCE OVERLAY --strict

API: scan_name_collisions(paths, *, type_db, types_from=()) -> dict.
Проверка лексическая: она не компилирует код и не вычисляет #if/load order.
Файлы и БД не изменяются; в JSON нет фрагментов исходного кода.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator


LAYER_NAMES = {
    1: "1_Core",
    2: "2_GameLib",
    3: "3_Game",
    4: "4_World",
    5: "5_Mission",
}
LAYER_RE = re.compile(r"(?:^|[/\\])([1-5])_(Core|GameLib|Game|World|Mission)(?=[/\\]|$)", re.I)
IDENT_RE = re.compile(r"[A-Za-z_]\w*\Z", re.ASCII)
TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|::|[^\s]", re.ASCII)
MODIFIERS = frozenset({
    "ref", "autoptr", "out", "inout", "notnull", "const", "static",
    "protected", "private", "public", "override", "proto", "native", "event",
})
BUILTINS = frozenset({
    "int", "float", "bool", "string", "vector", "typename", "void",
    "array", "map", "set", "Class",
})
LIMITATIONS = [
    "Lexical candidates only; this is not an Enforce compiler.",
    "Comments, literals and directive lines are masked; #if branches are not evaluated.",
    "Availability uses script-module layers, not actual CfgMods/load order or client/server guards.",
    "Unknown layers are review findings; types declared only in later layers are ignored.",
    "Types absent from the supplied DB/source snapshot cannot be checked.",
    "Macro-generated declarations and semantic alias resolution are not expanded.",
]


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    offset: int


@dataclass(frozen=True)
class TypeDecl:
    path: str
    line: int
    layer: str | None
    kind: str


def layer_from_path(path: str | Path) -> str | None:
    """Слой берётся из полного компонента пути, а не из похожего подстрочного имени."""
    match = LAYER_RE.search(str(path))
    if not match:
        return None
    number = int(match.group(1))
    if match.group(2).lower() != LAYER_NAMES[number].split("_", 1)[1].lower():
        return None
    return LAYER_NAMES[number]


def _rank(layer: str | None) -> int | None:
    return int(layer[0]) if layer in LAYER_NAMES.values() else None


def mask_noncode(source: str) -> str:
    """Маскирует комментарии, литералы и директивы, сохраняя позиции и строки."""
    chars = list(source)
    i = 0
    while i < len(source):
        start = i
        if source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = len(source) if end < 0 else end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = len(source) if end < 0 else end + 2
        elif source[i] in ('"', "'"):
            quote = source[i]
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            i = min(i, len(source))
        else:
            i += 1
            continue
        for pos in range(start, i):
            if chars[pos] not in "\r\n":
                chars[pos] = " "

    masked = "".join(chars)
    result = []
    continued = False
    for line in masked.splitlines(keepends=True):
        directive = continued or line.lstrip(" \t").startswith("#")
        continued = directive and line.rstrip("\r\n").rstrip(" \t").endswith("\\")
        result.append("".join(c if c in "\r\n" else " " for c in line) if directive else line)
    return "".join(result)


def tokenize(source: str) -> list[Token]:
    masked = mask_noncode(source)
    tokens = []
    line = 1
    previous = 0
    for match in TOKEN_RE.finditer(masked):
        line += masked.count("\n", previous, match.start())
        tokens.append(Token(match.group(), line, match.start()))
        previous = match.start()
    return tokens


def _paths(value: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(value, (str, Path)):
        value = [value]
    return [Path(path).resolve(strict=True) for path in value]


def source_files(paths: str | Path | Iterable[str | Path]) -> list[Path]:
    """Выбирает только явно заданные .c и .c внутри заданных каталогов."""
    files: set[Path] = set()
    for path in _paths(paths):
        if path.is_file():
            if path.suffix.lower() != ".c":
                raise ValueError(f"Expected .c source: {path}")
            files.add(path)
        elif path.is_dir():
            selected = {p.resolve() for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".c"}
            if not selected:
                raise ValueError(f"No .c sources in directory: {path}")
            files.update(selected)
        else:
            raise ValueError(f"Not a source file/directory: {path}")
    return sorted(files, key=lambda path: str(path).casefold())


def _read(path: Path) -> str:
    # Некорректная кодировка не должна скрывать имена или сдвигать позиции незаметно.
    return path.read_text(encoding="utf-8-sig")


def _typedef_end(tokens: list[Token], start: int) -> int:
    end = start + 1
    while end < len(tokens) and tokens[end].text not in (";", "{", "}"):
        end += 1
    return end


def _typedef_name(tokens: list[Token], start: int, end: int) -> Token | None:
    # Идентификатор в размере Alias[COUNT] или в generic-типе не является именем alias.
    depth = 0
    name = None
    for token in tokens[start + 1:end]:
        if token.text in ("<", "[", "("):
            depth += 1
        elif token.text in (">", "]", ")"):
            depth = max(0, depth - 1)
        elif depth == 0 and IDENT_RE.fullmatch(token.text):
            name = token
    return name


def source_type_declarations(source: str, path: str | Path) -> Iterator[tuple[str, TypeDecl]]:
    tokens = tokenize(source)
    layer = layer_from_path(path)
    for i, token in enumerate(tokens):
        if token.text in ("class", "enum") and i + 1 < len(tokens):
            name = tokens[i + 1]
            if IDENT_RE.fullmatch(name.text):
                yield name.text, TypeDecl(str(path), name.line, layer, token.text)
        elif token.text == "typedef":
            end = _typedef_end(tokens, i)
            name = _typedef_name(tokens, i, end)
            if name is not None:
                yield name.text, TypeDecl(str(path), name.line, layer, "typedef")


def load_type_index(type_db: str | Path) -> dict[str, list[TypeDecl]]:
    """Читает завершённый SQLite snapshot; открытие не создаёт БД/журналы."""
    db = Path(type_db).resolve(strict=True)
    if not db.is_file():
        raise ValueError(f"Not a type database: {db}")
    wal = Path(str(db) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("Type database has pending WAL; use a completed/checkpointed snapshot")
    result: dict[str, list[TypeDecl]] = {}
    # immutable предотвращает создание служебных SQLite-файлов при read-only работе.
    with sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"classes", "enums", "aliases"}.issubset(tables):
            raise ValueError("Type database must contain classes, enums and aliases tables")
        layers = dict(connection.execute("SELECT path, layer FROM files")) if "files" in tables else {}
        for table, kind in (("classes", "class"), ("enums", "enum"), ("aliases", "typedef")):
            for name, path, line in connection.execute(f"SELECT name, path, line FROM {table}"):
                if not name or not path:
                    continue
                owner_path = Path(path)
                if not owner_path.is_absolute():
                    owner_path = db.parent / "sources" / owner_path
                layer = layer_from_path(layers.get(path) or path)
                result.setdefault(name, []).append(TypeDecl(str(owner_path), int(line or 1), layer, kind))
    return result


def _generic_end(tokens: list[Token], start: int) -> int | None:
    depth = 0
    for i in range(start, len(tokens)):
        text = tokens[i].text
        if text == "<":
            depth += 1
        elif text == ">":
            depth -= 1
            if depth == 0:
                return i + 1
        elif text in (";", "{", "}", "(", ")", "="):
            return None
    return None


def _array_end(tokens: list[Token], start: int) -> int | None:
    depth = 0
    for i in range(start, len(tokens)):
        if tokens[i].text == "[":
            depth += 1
        elif tokens[i].text == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        elif tokens[i].text in (";", "{", "}"):
            return None
    return None


def _initializer_end(tokens: list[Token], start: int, valid_types: set[str]) -> int:
    """Пропускает initializer до разделителя declarator, учитывая вложенные вызовы."""
    closers = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    i = start
    while i < len(tokens):
        text = tokens[i].text
        if text == "<" and i and tokens[i - 1].text in valid_types:
            end = _generic_end(tokens, i)
            if end is not None:
                i = end
                continue
        if text in closers:
            stack.append(closers[text])
        elif text in ("}", "]", ")"):
            if not stack:
                return i
            if stack[-1] != text:
                return i
            stack.pop()
        elif not stack and text in (",", ";"):
            return i
        i += 1
    return i


def typed_declarations(source: str, known_types: set[str]) -> Iterator[Token]:
    """Находит поля/параметры/locals, в том числе for/foreach и простые списки."""
    tokens = tokenize(source)
    excluded: set[int] = set()
    for i, token in enumerate(tokens):
        if token.text == "typedef":
            excluded.update(range(i, _typedef_end(tokens, i) + 1))
    seen: set[int] = set()
    valid_types = known_types | BUILTINS
    # Параметры шаблона class Container<Class T> тоже допустимы как тип декларации.
    valid_types.update(tokens[i + 1].text for i, token in enumerate(tokens[:-1]) if token.text == "Class" and IDENT_RE.fullmatch(tokens[i + 1].text))

    for i, token in enumerate(tokens):
        if i in excluded or (i and tokens[i - 1].text in (".", "::")):
            continue
        j = i
        while j < len(tokens) and tokens[j].text in MODIFIERS:
            j += 1
        if j >= len(tokens) or tokens[j].text not in valid_types:
            continue
        j += 1
        if j < len(tokens) and tokens[j].text == "<":
            end = _generic_end(tokens, j)
            if end is None:
                continue
            j = end
        # Поддерживаются type[] name и type name[]; метод name(...) пропускается.
        while j < len(tokens) and tokens[j].text == "[":
            end = _array_end(tokens, j)
            if end is None:
                break
            j = end
        if j >= len(tokens) or not IDENT_RE.fullmatch(tokens[j].text):
            continue
        while j < len(tokens) and IDENT_RE.fullmatch(tokens[j].text):
            name = tokens[j]
            k = j + 1
            while k < len(tokens) and tokens[k].text == "[":
                end = _array_end(tokens, k)
                if end is None:
                    break
                k = end
            if k >= len(tokens) or tokens[k].text not in ("=", ";", ",", ")", ":"):
                break
            if name.offset not in seen:
                seen.add(name.offset)
                yield name
            if tokens[k].text == "=":
                k = _initializer_end(tokens, k + 1, valid_types)
                if k >= len(tokens):
                    break
            if tokens[k].text != ",":
                break
            # int first, Second; параметр с повторным типом разберётся следующим проходом.
            j = k + 1


def check_source(source: str, path: str | Path, type_index: dict[str, list[TypeDecl]]) -> list[dict]:
    """Проверяет переданный текст без записи; удобно для gate и регрессионных примеров."""
    layer = layer_from_path(path)
    rank = _rank(layer)
    findings = []
    for token in typed_declarations(source, set(type_index)):
        owners = type_index.get(token.text, [])
        available = [owner for owner in owners if rank is not None and _rank(owner.layer) is not None and _rank(owner.layer) <= rank]
        unknown = [owner for owner in owners if rank is None or _rank(owner.layer) is None]
        selected = available or unknown
        if not selected:
            continue
        owner = sorted(selected, key=lambda item: (_rank(item.layer) or 99, item.path.casefold(), item.line, item.kind))[0]
        findings.append({
            "path": str(path),
            "line": token.line,
            "name": token.text,
            "layer": layer,
            "severity": "conflict" if available else "review",
            "type_decl": asdict(owner),
        })
    return sorted(findings, key=lambda finding: (finding["path"].casefold(), finding["line"], finding["name"]))


def scan_name_collisions(
    paths: str | Path | Iterable[str | Path],
    *,
    type_db: str | Path,
    types_from: str | Path | Iterable[str | Path] = (),
) -> dict:
    """Главный API. Исключения ввода/БД не маскируются под успешную проверку."""
    index = load_type_index(type_db)
    checked = source_files(paths)
    if not checked:
        raise ValueError("No .c sources selected for checking")
    own_files = source_files(types_from)
    cache: dict[Path, str] = {}
    for path in own_files:
        text = _read(path)
        cache[path] = text
        for name, declaration in source_type_declarations(text, path):
            if declaration not in index.setdefault(name, []):
                index[name].append(declaration)
    findings = []
    for path in checked:
        text = cache[path] if path in cache else _read(path)
        findings.extend(check_source(text, path, index))
    conflicts = sum(finding["severity"] == "conflict" for finding in findings)
    reviews = len(findings) - conflicts
    return {
        "schema_version": 1,
        "check": "enforce_name_collisions",
        "status": "conflict" if conflicts else "review" if reviews else "pass",
        "files_checked": len(checked),
        "own_type_files": len(own_files),
        "type_names": len(index),
        "summary": {"conflicts": conflicts, "reviews": reviews},
        "findings": findings,
        "limitations": LIMITATIONS,
    }


def self_test() -> dict:
    """Регрессии полностью в памяти; временные исходники/БД не создаются."""
    from unittest.mock import patch

    index = {
        name: [TypeDecl(f"{layer}/Types.c", 10, layer, kind)]
        for name, layer, kind in (
            ("DamageType", "3_Game", "enum"), ("Component", "3_Game", "class"),
            ("Car", "3_Game", "class"), ("Weapon", "4_World", "class"),
            ("Radio", "4_World", "class"), ("Magazine", "4_World", "class"),
            ("Attachments", "5_Mission", "class"), ("AliasType", "3_Game", "typedef"),
            ("CarScript", "4_World", "class"),
        )
    }
    cases = [
        ("real_old_names", "4_World/Test.c", "int DamageType = 0; int Component = -1; CarScript Car;", {"DamageType", "Component", "Car"}, "conflict"),
        ("scoped_renames", "4_World/Test.c", "int HitDamageType = 0; int HitComponent = -1; CarScript ReservedCar;", set(), None),
        ("later_layer_names", "3_Game/Test.c", "string Weapon; bool Radio; string Magazine; array<string> Attachments;", set(), None),
        ("params_loops_locals", "4_World/Test.c", "void F(out int DamageType) { for (int Component = 0; Component < 1; Component++) {} foreach (CarScript Car : cars) {} }", {"DamageType", "Component", "Car"}, "conflict"),
        ("mask_comments_literals", "4_World/Test.c", '// int DamageType;\n/* int Component; */ string s = "CarScript Car;"; string c = \'x\';', set(), None),
        ("mask_directives", "4_World/Test.c", "#define F int DamageType; \\\nint Component;\nint Safe;", set(), None),
        ("case_sensitive", "4_World/Test.c", "int damageType; int component; CarScript car;", set(), None),
        ("unknown_layer", "Unknown/Test.c", "int DamageType;", {"DamageType"}, "review"),
        ("generic_arrays", "4_World/Test.c", "ref array<ref CarScript> Car; int Component[3];", {"Car", "Component"}, "conflict"),
        ("methods_not_variables", "4_World/Test.c", "int DamageType() { return 0; } void F() { new Component; return Component.Cast(value); }", set(), None),
        ("aliases_not_variables", "4_World/Test.c", "typedef array<string> AliasType; int AliasType;", {"AliasType"}, "conflict"),
        ("multiple_declarators", "4_World/Test.c", "int safe, Component, DamageType;", {"Component", "DamageType"}, "conflict"),
        ("initialized_declarators", "4_World/Test.c", "int safe = F(1, 2), DamageType = 1, Component[3] = { 1, 2, 3 };", {"Component", "DamageType"}, "conflict"),
        ("generic_parameter_type", "4_World/Test.c", "class Own<Class T> { T DamageType; }", {"DamageType"}, "conflict"),
    ]
    passed = []
    for name, path, source, expected, severity in cases:
        findings = check_source(source, path, index)
        assert {finding["name"] for finding in findings} == expected, name
        assert all(finding["severity"] == severity for finding in findings), name
        assert all("snippet" not in finding for finding in findings), name
        passed.append(name)
    own = dict(source_type_declarations("class OwnType {} enum OwnEnum { A } typedef array<ref OwnType> OwnAlias;", "4_World/Own.c"))
    assert set(own) == {"OwnType", "OwnEnum", "OwnAlias"}
    assert check_source("int OwnType; int OwnEnum; int OwnAlias;", "4_World/Use.c", {key: [value] for key, value in own.items()})
    passed.append("own_class_enum_typedef")
    aliases = dict(source_type_declarations("typedef int SomeAlias[COUNT]; typedef map<string, int> OtherAlias;", "4_World/Own.c"))
    assert set(aliases) == {"SomeAlias", "OtherAlias"}
    passed.append("typedef_symbolic_dimensions")
    assert layer_from_path("x/3_World/Test.c") is None
    passed.append("invalid_layer_is_unknown")
    with patch(f"{__name__}.load_type_index", return_value={}):
        try:
            scan_name_collisions([], type_db="unused")
        except ValueError:
            passed.append("empty_selection_is_error")
        else:
            raise AssertionError("empty_selection_is_error")
    with patch.object(Path, "rglob", return_value=iter(())):
        try:
            source_files(Path(__file__).parent)
        except ValueError:
            passed.append("explicit_empty_directory_is_error")
        else:
            raise AssertionError("explicit_empty_directory_is_error")
    return {"status": "pass", "tests_passed": len(passed), "tests": passed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type-db", type=Path, help="Read-only vanilla symbols.sqlite snapshot")
    parser.add_argument("--types-from", type=Path, action="append", default=[], help="Full own source; repeat for additional roots")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on conflicts or unknown-layer review findings")
    parser.add_argument("--self-test", action="store_true", help="Run built-in regressions in memory")
    parser.add_argument("paths", nargs="*", type=Path, help="Own .c files/directories to check")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            result = self_test()
        else:
            if args.type_db is None or not args.paths:
                parser.error("--type-db and at least one source path are required")
            result = scan_name_collisions(args.paths, type_db=args.type_db, types_from=args.types_from)
    except (OSError, ValueError, sqlite3.Error, AssertionError) as error:
        print(json.dumps({"check": "enforce_name_collisions", "status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and result["status"] in ("conflict", "review") else 0


if __name__ == "__main__":
    sys.exit(main())
