#!/usr/bin/env python3
"""Read-only review '%' в явно смешанном float/vector контексте DayZ Enforce.

API: scan_numeric_context(paths, *, type_db=None, types_from=()) -> dict.
CLI: python enforce_numeric_context.py --type-db symbols.sqlite \
    --types-from FULL_SOURCE SOURCE_OR_OVERLAY --strict

Целочисленный '%' поддерживается DayZ и здесь не запрещается. Находка — риск
смешанного контекста, а не доказательство конкретного implicit promotion.
Math.ModFloat не предлагается как универсальная замена целочисленного остатка.
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


LEX = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27')
TOKEN_RE = re.compile(r"[A-Za-z_]\w*|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?|&&|\|\||==|!=|<=|>=|::|[^\s]", re.ASCII)
IDENT = re.compile(r"[A-Za-z_]\w*\Z", re.ASCII)
NUMBER = re.compile(r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?\Z")
NUMERIC = {"int", "float", "vector"}
LIMITATIONS = [
    "Review heuristic only; mixed-context findings do not establish exact compiler promotion semantics.",
    "Integer modulo is supported; integer-only conditions, timestamps and leap-year calculations are not banned or converted.",
    "Only explicit numeric literals, primitive declarations/parameters/fields and simple assignment/return context are inspected.",
    "Complex receiver expressions, overload return types, macros and preprocessor branches are not compiled or fully inferred.",
    "Evidence in one statement can conservatively include independent function arguments; findings require source review.",
    "ModFloat is not a universal integer-modulo replacement, especially for large UTC integers.",
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
    body: int
    end: int
    returns: str


def tokenize(source):
    code = LEX.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), source)
    lines = []
    continued = False
    for line in code.splitlines(keepends=True):
        directive = continued or line.lstrip(" \t").startswith("#")
        continued = directive and line.rstrip("\r\n").rstrip(" \t").endswith("\\")
        lines.append("".join(char if char in "\r\n" else " " for char in line) if directive else line)
    code = "".join(lines)
    result = []
    previous = 0
    line = 1
    for match in TOKEN_RE.finditer(code):
        line += code.count("\n", previous, match.start())
        result.append(Token(match.group(), line, match.start()))
        previous = match.start()
    return result


def source_files(paths):
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


def pairs_for(tokens, path):
    result = {}
    stack = []
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


def model_for(source, path):
    tokens = tokenize(source)
    pairs = pairs_for(tokens, path)
    classes = {}
    parents = {}
    for i, token in enumerate(tokens[:-1]):
        if token.text != "class" or not IDENT.fullmatch(tokens[i + 1].text):
            continue
        name = tokens[i + 1].text
        j = i + 2
        while j < len(tokens) and tokens[j].text not in ("{", ";"):
            if tokens[j].text in ("extends", ":") and j + 1 < len(tokens):
                parents[name] = tokens[j + 1].text
            j += 1
        if j < len(tokens) and tokens[j].text == "{" and j in pairs:
            classes[j] = (name, pairs[j])
    braces = [(i, pairs[i]) for i, token in enumerate(tokens) if token.text == "{" and i in pairs]
    methods = []
    controls = {"if", "for", "foreach", "while", "switch", "catch", "Cast"}
    for opening, token in enumerate(tokens):
        if token.text != "(" or opening not in pairs or opening == 0:
            continue
        closing = pairs[opening]
        body = closing + 1
        if body not in pairs or tokens[body].text != "{":
            continue
        name = tokens[opening - 1].text
        before = tokens[opening - 2].text if opening > 1 else ""
        if not IDENT.fullmatch(name) or name in controls or before in (".", "::", "=", "new"):
            continue
        containers = [(start, end) for start, end in braces if start < opening < end]
        parent = min(containers, key=lambda interval: interval[1] - interval[0])[0] if containers else None
        if parent is not None and parent not in classes:
            continue
        owner = classes[parent][0] if parent is not None else ""
        if name != owner and not (IDENT.fullmatch(before) or before in (">", "]")):
            continue
        methods.append(Method(name, owner, opening, closing, body, pairs[body], before if before in NUMERIC else ""))
    declarations = []
    for i in range(len(tokens) - 2):
        if tokens[i].text in NUMERIC and IDENT.fullmatch(tokens[i + 1].text) and tokens[i + 2].text in ("=", ";", ",", ")", ":"):
            declarations.append((i + 1, tokens[i + 1].text, tokens[i].text))
    fields = {}
    for index, name, typ in declarations:
        if any(method.opening < index < method.end for method in methods):
            continue
        owners = [(start, owner, end) for start, (owner, end) in classes.items() if start < index < end]
        if owners:
            _, owner, _ = min(owners, key=lambda item: item[2] - item[0])
            fields[(owner, name)] = typ
    return {"tokens": tokens, "pairs": pairs, "methods": methods, "classes": classes, "parents": parents, "declarations": declarations, "fields": fields}


def _numeric_root(value):
    if "[" in value or "<" in value:
        return ""
    words = value.split()
    return next((word for word in words if word in NUMERIC), "")


def load_context(type_db):
    parents = {}
    fields = {}
    if type_db is not None:
        db = Path(type_db).resolve(strict=True)
        wal = Path(str(db) + "-wal")
        if not db.is_file() or (wal.exists() and wal.stat().st_size):
            raise ValueError("Expected completed type DB without pending WAL")
        with sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            for name, base in connection.execute("SELECT name,base FROM classes WHERE base!=''"):
                parents[name] = base
            for owner, name, type_name in connection.execute("SELECT owner,name,type FROM fields"):
                typ = _numeric_root(type_name or "")
                if typ:
                    fields[(owner, name)] = typ
    return parents, fields


def _statement_bounds(tokens, pairs, index):
    # Тип результата array[index] не меняет целочисленный контекст самого index.
    indexes = [(opening, closing) for opening, closing in pairs.items() if opening < index < closing and tokens[opening].text == "["]
    if indexes:
        opening, closing = min(indexes, key=lambda interval: interval[1] - interval[0])
        return opening + 1, closing
    start = index
    while start and tokens[start - 1].text not in (";", "{", "}"):
        start -= 1
    end = index + 1
    while end < len(tokens) and tokens[end].text not in (";", "{", "}"):
        end += 1
    for i in range(start, index):
        if tokens[i].text in ("if", "while", "for", "foreach") and i + 1 in pairs:
            close = pairs[i + 1]
            if index > close:
                start = close + 1
            elif i + 1 < index < close:
                start, end = i + 2, min(end, close)
    # Float-сравнение в соседнем &&/|| ответвлении не является контекстом int %.
    for i in range(start, end):
        if tokens[i].text in ("&&", "||"):
            if i < index:
                start = i + 1
            elif i > index:
                end = i
                break
    return start, end


def check_model(model, path, parents=None, fields=None):
    parents = parents or {}
    fields = fields or {}
    tokens = model["tokens"]
    findings = []
    unresolved = []
    counts = Counter(modulo_tokens=0, integer_only=0, mixed_review=0, unresolved=0)
    for index, token in enumerate(tokens):
        if token.text != "%":
            continue
        counts["modulo_tokens"] += 1
        method = next((method for method in model["methods"] if method.body < index < method.end), None)
        owner = method.owner if method else ""

        def symbol_type(name, position):
            candidates = [(at, typ) for at, declared_name, typ in model["declarations"] if declared_name == name and at <= position and method is not None and method.opening < at < method.end]
            if candidates:
                return max(candidates)[1]
            seen = set()
            ancestor = owner
            while ancestor and ancestor not in seen:
                seen.add(ancestor)
                if (ancestor, name) in fields:
                    return fields[(ancestor, name)]
                ancestor = parents.get(ancestor, "")
            return ""

        start, end = _statement_bounds(tokens, model["pairs"], index)
        reasons = set()
        symbols = []
        for i in range(start, end):
            text = tokens[i].text
            if NUMBER.fullmatch(text) and any(char in text.lower() for char in (".", "e")):
                reasons.add("float_literal_in_context")
            elif IDENT.fullmatch(text):
                typ = symbol_type(text, i)
                if typ in ("float", "vector"):
                    reasons.add("typed_" + typ + "_in_context")
                    item = {"name": text, "type": typ, "line": tokens[i].line}
                    if item not in symbols:
                        symbols.append(item)
        if method and method.returns in ("float", "vector") and any(tokens[i].text == "return" for i in range(start, index)):
            reasons.add("typed_" + method.returns + "_return")
        if reasons:
            counts["mixed_review"] += 1
            findings.append({"path": str(path), "line": token.line, "method": method.name if method else None, "operator": "%", "reasons": sorted(reasons), "symbols": symbols})
            continue

        def operand_type(at, direction):
            if not 0 <= at < len(tokens):
                return ""
            text = tokens[at].text
            if NUMBER.fullmatch(text):
                return "float" if any(char in text.lower() for char in (".", "e")) else "int"
            if IDENT.fullmatch(text):
                return symbol_type(text, at)
            if text == ")" and direction < 0 and at in model["pairs"]:
                opening = model["pairs"][at]
                if opening and IDENT.fullmatch(tokens[opening - 1].text):
                    return ""
                types = [operand_type(i, 0) for i in range(opening + 1, at) if IDENT.fullmatch(tokens[i].text) or NUMBER.fullmatch(tokens[i].text)]
                return "int" if types and all(typ == "int" for typ in types) else ""
            return ""

        if operand_type(index - 1, -1) == operand_type(index + 1, 1) == "int":
            counts["integer_only"] += 1
        else:
            counts["unresolved"] += 1
            unresolved.append({"path": str(path), "line": token.line, "reason": "operand_types_not_resolved"})
    return {"counts": dict(counts), "findings": findings, "unresolved": unresolved}


def scan_numeric_context(paths: str | Path | Iterable[str | Path], *, type_db=None, types_from=()):
    checked = source_files(paths)
    if not checked:
        raise ValueError("No .c sources selected for checking")
    own_files = source_files(types_from)
    models = {path: model_for(path.read_text(encoding="utf-8-sig"), path) for path in set(checked) | set(own_files)}
    parents, fields = load_context(type_db)
    for path in own_files:
        parents.update(models[path]["parents"])
        fields.update(models[path]["fields"])
    counts = Counter(modulo_tokens=0, integer_only=0, mixed_review=0, unresolved=0)
    findings = []
    unresolved = []
    for path in checked:
        local_fields = {**fields, **models[path]["fields"]}
        report = check_model(models[path], path, {**parents, **models[path]["parents"]}, local_fields)
        counts.update(report["counts"])
        findings.extend(report["findings"])
        unresolved.extend(report["unresolved"])
    return {"schema_version": 1, "check": "enforce_numeric_context", "status": "review" if findings else "pass", "files_checked": len(checked), "own_type_files": len(own_files), "counts": dict(counts), "findings": findings, "unresolved": unresolved, "limitations": LIMITATIONS}


def self_test():
    failed = "void F(int index, vector death) { vector candidate = death + DirectionFromAngles(yaw, 0) * (0.35 + 0.15 * (index % 4)); }"
    fixed = failed.replace("index % 4", "Math.ModFloat(index, 4)")
    cases = [
        ("actual_failed_formula", failed, 1),
        ("corrected_modfloat", fixed, 0),
        ("integer_time_large_utc", "void F(int seconds) { int daytime = seconds % 86400; int minute = (daytime % 3600) / 60; int second = daytime % 60; }", 0),
        ("leap_year", "bool F(int year) { return year % 4 == 0 && (year % 100 != 0 || year % 400 == 0); }", 0),
        ("integer_loop_condition", "void F(int i, float distance) { if (distance < 1.5 && i % 2 == 0) {} }", 0),
        ("float_operand", "void F(float value) { int result = value % 4; }", 1),
        ("float_result", "void F(int value) { float result = value % 4; }", 1),
        ("float_return", "float F(int value) { return value % 4; }", 1),
        ("float_literal_operand", "void F(int value) { int result = value % 4.0; }", 1),
        ("vector_operand", "void F(vector value) { int result = value % 4; }", 1),
        ("format_comments_mask", 'void F() { string s = string.Format("%1 %2", 1.5, 2.5); /* float x=i%4; */ // float y=i%4;\n }', 0),
        ("separated_int_then_float", "void F(int index) { int groundRing = index % 4; float radius = 0.35 + 0.15 * groundRing; }", 0),
        ("vector_array_integer_index", "void F(int index, int count) { vector post = points[index % count]; }", 0),
    ]
    passed = []
    for label, source, expected in cases:
        model = model_for(source, "4_World/Test.c")
        report = check_model(model, "4_World/Test.c", fields=model["fields"])
        assert len(report["findings"]) == expected, (label, report)
        passed.append(label)
    utc = check_model(model_for("void F() { int seconds = 2147483000; int day = seconds % 86400; }", "4_World/Test.c"), "4_World/Test.c")
    assert utc["counts"]["integer_only"] == 1 and not utc["findings"]
    passed.append("large_utc_not_converted")
    for label, call in (("empty_input_error", lambda: scan_numeric_context([])), ("unbalanced_input_error", lambda: model_for("void F() {", "4_World/Test.c"))):
        try:
            call()
        except ValueError:
            passed.append(label)
        else:
            raise AssertionError(label)
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
            report = scan_numeric_context(args.paths, type_db=args.type_db, types_from=args.types_from)
    except (OSError, ValueError, sqlite3.Error, AssertionError) as error:
        print(json.dumps({"check": "enforce_numeric_context", "status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and report["status"] == "review" else 0


if __name__ == "__main__":
    sys.exit(main())
