#!/usr/bin/env python3
"""Read-only проверка повторных имён параметров/локалов в методах DayZ Enforce.

API: scan_local_declarations(paths, *, type_db=None, types_from=()) -> dict.
CLI: python enforce_local_declarations.py --type-db symbols.sqlite \
    --types-from FULL_SOURCE SOURCE_OR_OVERLAY --strict

Повторы внутри одного метода — консервативное правило собственного кода.
Это не утверждение об общей области видимости чтения переменных: она блочная.
Препроцессор не вычисляется; имена полей и разных методов не объединяются.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Iterable


IDENT = re.compile(r"[A-Za-z_]\w*\Z", re.ASCII)
LEX = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27')
TOKENS = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|::|[^\s]", re.ASCII)
MODIFIERS = set("ref autoptr out inout notnull const static protected private public override proto native event owned volatile".split())
KEYWORDS = set("if else for foreach while switch case return new delete class enum typedef extends break continue do true false null super this throw catch try".split())
BUILTINS = set("int float bool string vector typename void array map set Class auto func".split())
LIMITATIONS = [
    "Method-wide repeated-declaration policy only; variable read visibility remains block-specific.",
    "DayZ rejects documented if/else and nested/for redeclarations; all repeated names in a method are conservatively flagged.",
    "Preprocessor conditions/macros are not evaluated; mutually exclusive branches require source review.",
    "Fields and different methods are kept separate; no public field or override parameter is renamed.",
    "This lexical checker is not an Enforce compiler or complete declaration grammar.",
    "Generic type roots require supplied/own type context; arbitrary comparison expressions are not declarations.",
]


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    offset: int


@dataclass(frozen=True)
class Method:
    name: str
    owner: str
    opening: int
    closing: int
    body: int | None
    end: int
    line: int


def tokenize(source: str) -> list[Token]:
    code = LEX.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), source)
    lines = []
    continued = False
    for line in code.splitlines(keepends=True):
        directive = continued or line.lstrip(" \t").startswith("#")
        continued = directive and line.rstrip("\r\n").rstrip(" \t").endswith("\\")
        lines.append("".join(char if char in "\r\n" else " " for char in line) if directive else line)
    code = "".join(lines)
    line = 1
    previous = 0
    result = []
    for match in TOKENS.finditer(code):
        line += code.count("\n", previous, match.start())
        result.append(Token(match.group(), line, match.start()))
        previous = match.start()
    return result


def source_files(paths: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    result = set()
    for item in paths:
        path = Path(item).resolve(strict=True)
        if path.is_file() and path.suffix.lower() == ".c":
            result.add(path)
        elif path.is_dir():
            selected = {candidate.resolve() for candidate in path.rglob("*") if candidate.is_file() and candidate.suffix.lower() == ".c"}
            if not selected:
                raise ValueError(f"No .c sources in directory: {path}")
            result.update(selected)
        else:
            raise ValueError(f"Expected .c source file or directory: {path}")
    return sorted(result, key=lambda path: str(path).casefold())


def token_pairs(tokens: list[Token], path="<memory>") -> dict[int, int]:
    stack = []
    result = {}
    closers = {"(": ")", "[": "]", "{": "}"}
    for i, token in enumerate(tokens):
        if token.text in closers:
            stack.append((closers[token.text], i))
        elif token.text in ("}", "]", ")"):
            if not stack or stack[-1][0] != token.text:
                raise ValueError(f"Unbalanced source delimiters: {path}:{token.line}")
            _, opening = stack.pop()
            result[opening] = i
            result[i] = opening
    if stack:
        raise ValueError(f"Unbalanced source delimiters: {path}:{tokens[stack[-1][1]].line}")
    return result


def _angle_end(tokens, start):
    depth = 0
    for i in range(start, len(tokens)):
        text = tokens[i].text
        if text == "<":
            depth += 1
        elif text == ">":
            depth -= 1
            if not depth:
                return i + 1
        elif text in (";", "{", "}", "(", ")", "="):
            return None
    return None


def _classes(tokens, pairs):
    result = {}
    for i, token in enumerate(tokens[:-1]):
        if token.text != "class" or not IDENT.fullmatch(tokens[i + 1].text):
            continue
        j = i + 2
        while j < len(tokens) and tokens[j].text not in ("{", ";"):
            j += 1
        if j < len(tokens) and tokens[j].text == "{" and j in pairs:
            result[j] = (tokens[i + 1].text, pairs[j])
    return result


def method_ranges(tokens: list[Token], pairs: dict[int, int]) -> list[Method]:
    classes = _classes(tokens, pairs)
    braces = [(i, pairs[i]) for i, token in enumerate(tokens) if token.text == "{" and i in pairs]
    methods = []
    for opening, token in enumerate(tokens):
        if token.text != "(" or opening not in pairs or opening == 0:
            continue
        closing = pairs[opening]
        tail = closing + 1
        if tail >= len(tokens) or tokens[tail].text not in ("{", ";"):
            continue
        name = tokens[opening - 1].text
        before = tokens[opening - 2].text if opening > 1 else ""
        if not IDENT.fullmatch(name) or name in KEYWORDS or before in (".", "::", "=", "new"):
            continue
        containers = [(start, end) for start, end in braces if start < opening < end]
        parent = min(containers, key=lambda interval: interval[1] - interval[0])[0] if containers else None
        if parent is not None and parent not in classes:
            continue
        owner = classes[parent][0] if parent is not None else ""
        constructor = name == owner
        if not constructor and not (IDENT.fullmatch(before) and before not in KEYWORDS or before in (">", "]")):
            continue
        body = tail if tokens[tail].text == "{" else None
        if body is not None and body not in pairs:
            raise ValueError("Method body delimiter was not matched")
        methods.append(Method(name, owner, opening, closing, body, pairs[body] if body is not None else closing, tokens[opening - 1].line))
    return methods


def _initializer_end(tokens, pairs, start, known_types):
    i = start
    while i < len(tokens):
        text = tokens[i].text
        if text in ("(", "[", "{") and i in pairs:
            i = pairs[i] + 1
            continue
        if text == "<" and i and tokens[i - 1].text in known_types:
            end = _angle_end(tokens, i)
            if end is not None:
                i = end
                continue
        if text in (",", ";", ")", "}"):
            break
        i += 1
    return i


def declarations(tokens, pairs, known_types):
    """Сохраняет разные declaration tokens даже при одинаковых имени и строке."""
    seen = set()
    excluded = set()
    for i, token in enumerate(tokens):
        if token.text == "typedef":
            j = i
            while j < len(tokens) and tokens[j].text != ";":
                excluded.add(j)
                j += 1
    unresolved = []
    values = []
    for i, token in enumerate(tokens):
        if i in excluded or (i and tokens[i - 1].text in (".", "::")):
            continue
        j = i
        while j < len(tokens) and tokens[j].text in MODIFIERS:
            j += 1
        if j >= len(tokens) or not IDENT.fullmatch(tokens[j].text) or tokens[j].text in KEYWORDS:
            continue
        type_name = tokens[j].text
        j += 1
        if j < len(tokens) and tokens[j].text == "<":
            if type_name not in known_types:
                continue
            end = _angle_end(tokens, j)
            if end is None:
                if type_name in known_types:
                    unresolved.append({"token_index": i, "line": token.line, "reason": "unparsed_generic_declaration"})
                continue
            j = end
        while j < len(tokens) and tokens[j].text == "[" and j in pairs:
            j = pairs[j] + 1
        while j < len(tokens) and IDENT.fullmatch(tokens[j].text):
            name = tokens[j]
            k = j + 1
            while k < len(tokens) and tokens[k].text == "[" and k in pairs:
                k = pairs[k] + 1
            if k >= len(tokens) or tokens[k].text not in ("=", ";", ",", ")", ":"):
                break
            if j not in seen:
                seen.add(j)
                values.append({"name": name.text, "type": type_name, "line": name.line, "token_index": j})
            if tokens[k].text == "=":
                k = _initializer_end(tokens, pairs, k + 1, known_types)
            if k >= len(tokens) or tokens[k].text != ",":
                break
            j = k + 1
    return values, unresolved


def check_source(source: str, path: str | Path, known_types=()) -> dict:
    tokens = tokenize(source)
    pairs = token_pairs(tokens, str(path))
    methods = method_ranges(tokens, pairs)
    values, unresolved = declarations(tokens, pairs, set(known_types) | BUILTINS)
    findings = []
    groups = defaultdict(list)
    local_count = 0
    for declaration in values:
        index = declaration["token_index"]
        method = next((method for method in methods if method.opening < index < method.end), None)
        if method is None:
            continue
        local_count += 1
        kind = "parameter" if index < method.closing else "local"
        groups[(method, declaration["name"])].append({"line": declaration["line"], "kind": kind, "token_index": index})
    for (method, name), items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: item["token_index"])
        findings.append({"path": str(path), "line": items[1]["line"], "owner": method.owner, "method": method.name, "method_line": method.line, "name": name, "declarations": [{"line": item["line"], "kind": item["kind"]} for item in items]})
    reviews = []
    seen_reviews = set()
    for item in unresolved:
        method = next((method for method in methods if method.opening < item["token_index"] < method.end), None)
        key = (item["line"], item["reason"])
        if method is not None and key not in seen_reviews:
            seen_reviews.add(key)
            reviews.append({"path": str(path), "line": item["line"], "method": method.name, "reason": item["reason"]})
    return {"methods_checked": len(methods), "declarations_checked": local_count, "findings": sorted(findings, key=lambda finding: (finding["line"], finding["name"])), "unresolved": reviews}


def _known_types(type_db, texts):
    known = set(BUILTINS)
    if type_db is not None:
        db = Path(type_db).resolve(strict=True)
        wal = Path(str(db) + "-wal")
        if not db.is_file() or (wal.exists() and wal.stat().st_size):
            raise ValueError("Expected completed type DB without pending WAL")
        with sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"classes", "enums", "aliases"}.issubset(tables):
                raise ValueError("Type DB must contain classes/enums/aliases tables")
            for table in ("classes", "enums", "aliases"):
                known.update(row[0] for row in connection.execute(f"SELECT name FROM {table}"))
    for text in texts:
        tokens = tokenize(text)
        known.update(tokens[i + 1].text for i, token in enumerate(tokens[:-1]) if token.text in ("class", "enum"))
    return known


def scan_local_declarations(paths: str | Path | Iterable[str | Path], *, type_db=None, types_from=()) -> dict:
    checked = source_files(paths)
    if not checked:
        raise ValueError("No .c sources selected for checking")
    own_files = source_files(types_from)
    texts = {path: path.read_text(encoding="utf-8-sig") for path in set(checked) | set(own_files)}
    known = _known_types(type_db, texts.values())
    counts = Counter(methods=0, declarations=0)
    findings = []
    unresolved = []
    for path in checked:
        report = check_source(texts[path], path, known)
        counts["methods"] += report["methods_checked"]
        counts["declarations"] += report["declarations_checked"]
        findings.extend(report["findings"])
        unresolved.extend(report["unresolved"])
    return {"schema_version": 1, "check": "enforce_local_declarations", "status": "conflict" if findings else "review" if unresolved else "pass", "files_checked": len(checked), "own_type_files": len(own_files), "counts": dict(counts), "findings": findings, "unresolved": unresolved, "limitations": LIMITATIONS}


def self_test():
    cases = [
        ("siblings_foreach", "void F() { if (ready) { foreach (Object unit : a) {} } else { foreach (Object unit : b) {} } }", ["unit"]),
        ("parameter_local", "void F(Object unit) { Object unit; }", ["unit"]),
        ("comma_initialized", "void F() { int unit = Call(1, 2), other = 0; int unit; }", ["unit"]),
        ("for_nested", "void F() { int i; for (int i = 0; i < 3; i++) {} }", ["i"]),
        ("different_methods", "void A(Object unit) {} void B(Object unit) {}", []),
        ("field_and_local", "class C { Object unit; void F(Object unit) {} }", []),
        ("comments_strings", 'void F() { Object unit; // Object unit;\n string s = "Object unit;"; /* Object unit; */ }', []),
        ("renamed_foreach", "void F() { if (ready) { foreach (Object unit : a) {} } else { foreach (Object followingUnit : b) {} } }", []),
        ("nested_generic_arrays", "void F() { ref map<string, ref array<ref Object>> unit; Object unit[2]; }", ["unit"]),
        ("same_line_distinct_tokens", "void F() { int unit, unit; }", ["unit"]),
        ("constructor", "class C { void C(Object unit) { Object unit; } }", ["unit"]),
        ("prototype_parameters", "class C { proto void F(int unit, int unit); }", ["unit"]),
        ("unknown_type_shape", "void F() { Unknown unit; Unknown unit; }", ["unit"]),
        ("comparisons_not_declarations", "void F(float width, float height) { if (x < 0 || y < 0 || x > width || y > height) {} if (x < 2 || x > height) {} }", []),
        ("directive_mask", "#define F Object unit; \\\nObject unit;\nvoid F() { Object unit; }", []),
    ]
    passed = []
    for label, source, expected in cases:
        report = check_source(source, "4_World/Test.c", {"Object"})
        assert [finding["name"] for finding in report["findings"]] == expected, (label, report)
        assert not report["unresolved"], (label, report)
        passed.append(label)
    report = check_source("void F() { array<Object unit; }", "4_World/Test.c", {"Object"})
    assert report["unresolved"]
    passed.append("malformed_generic_requires_review")
    try:
        check_source("void F() {", "4_World/Test.c")
    except ValueError:
        passed.append("unbalanced_input_is_error")
    else:
        raise AssertionError("unbalanced_input_is_error")
    try:
        scan_local_declarations([])
    except ValueError:
        passed.append("empty_selection_is_error")
    else:
        raise AssertionError("empty_selection_is_error")
    return {"status": "pass", "tests_passed": len(passed), "tests": passed, "limitations": LIMITATIONS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type-db", type=Path)
    parser.add_argument("--types-from", type=Path, action="append", default=[])
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            report = self_test()
        else:
            if not args.paths:
                parser.error("At least one source path is required")
            report = scan_local_declarations(args.paths, type_db=args.type_db, types_from=args.types_from)
    except (OSError, ValueError, sqlite3.Error, AssertionError) as error:
        print(json.dumps({"check": "enforce_local_declarations", "status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and report["status"] in ("conflict", "review") else 0


if __name__ == "__main__":
    sys.exit(main())
