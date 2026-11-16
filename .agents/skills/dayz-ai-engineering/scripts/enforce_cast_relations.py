#!/usr/bin/env python3
"""Read-only поиск явно несовместимых Class.Cast(identifier) в Enforce.

CLI:
    python enforce_cast_relations.py --type-db symbols.sqlite \
        --types-from FULL_SOURCE SOURCE_OR_OVERLAY --strict

API: scan_cast_relations(paths, *, type_db, types_from=()) -> dict.
Это лексическая проверка известных классов, не компилятор и не проверка runtime.
Неизвестные типы/выражения учитываются как unresolved и не блокируют --strict.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Iterable

BUILTINS = frozenset({"int", "float", "bool", "string", "vector", "typename", "void", "array", "map", "set", "Class"})
MODIFIERS = frozenset({"ref", "autoptr", "out", "inout", "notnull", "const", "static", "protected", "private", "public", "override", "proto", "native", "event"})
IDENT_RE = re.compile(r"[A-Za-z_]\w*\Z", re.ASCII)
TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|::|[^\s]", re.ASCII)
LAYER_NAMES = {1: "1_Core", 2: "2_GameLib", 3: "3_Game", 4: "4_World", 5: "5_Mission"}
LAYER_RE = re.compile(r"(?:^|[/\\])([1-5])_(Core|GameLib|Game|World|Mission)(?=[/\\]|$)", re.I)


LIMITATIONS = [
    "Lexical Class.Cast(identifier) checks only; this is not an Enforce compiler.",
    "Complex arguments, unknown/ambiguous types and incomplete unrelated ancestry are unresolved, not errors.",
    "Preprocessor branches, macros, actual load order and custom Cast implementations are not evaluated.",
    "Scopes cover braces, method parameters, fields, locals and for/foreach headers; arbitrary syntax is not parsed.",
    "Primitive/container/alias types are not treated as classes; semantic alias expansion is not performed.",
    "Class availability uses module layers inferred from paths, not active CfgMods or execution side.",
]


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    offset: int


def layer_from_path(path: str | Path) -> str | None:
    match = LAYER_RE.search(str(path))
    if not match:
        return None
    number = int(match.group(1))
    if match.group(2).lower() != LAYER_NAMES[number].split("_", 1)[1].lower():
        return None
    return LAYER_NAMES[number]


def tokenize(source: str) -> list[Token]:
    """Маскирует комментарии, литералы и строки директив, не сдвигая позиции."""
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
    lines = []
    continued = False
    for line in "".join(chars).splitlines(keepends=True):
        directive = continued or line.lstrip(" \t").startswith("#")
        continued = directive and line.rstrip("\r\n").rstrip(" \t").endswith("\\")
        lines.append("".join(char if char in "\r\n" else " " for char in line) if directive else line)
    masked = "".join(lines)
    tokens = []
    previous = 0
    line = 1
    for match in TOKEN_RE.finditer(masked):
        line += masked.count("\n", previous, match.start())
        tokens.append(Token(match.group(), line, match.start()))
        previous = match.start()
    return tokens


def source_files(paths: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    result: set[Path] = set()
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
            raise ValueError(f"Expected .c source file or source directory: {path}")
    return sorted(result, key=lambda path: str(path).casefold())


@dataclass(frozen=True)
class Declaration:
    path: str
    line: int
    kind: str


@dataclass
class ClassInfo:
    declarations: list[Declaration] = field(default_factory=list)
    bases: set[str] = field(default_factory=set)
    root: bool = False
    layers: set[str | None] = field(default_factory=set)


@dataclass(frozen=True)
class Variable:
    name: str
    type_name: str
    token_index: int
    declaration: Declaration


@dataclass
class Scope:
    start: int
    end: int
    kind: str = "block"
    owner: str | None = None
    parent: int | None = None
    variables: dict[str, list[Variable]] = field(default_factory=dict)


@dataclass
class SourceModel:
    path: str
    tokens: list[Token]
    pairs: dict[int, int]
    scopes: list[Scope]

    def scope_at(self, index: int) -> int:
        candidates = [i for i, scope in enumerate(self.scopes) if scope.start <= index <= scope.end]
        return min(candidates, key=lambda i: (self.scopes[i].end - self.scopes[i].start, -self.scopes[i].start))

    def owner_at(self, index: int) -> str | None:
        scope_id: int | None = self.scope_at(index)
        while scope_id is not None:
            scope = self.scopes[scope_id]
            if scope.kind == "class":
                return scope.owner
            scope_id = scope.parent
        return None


def _pairs(tokens: list[Token]) -> dict[int, int]:
    stack: list[tuple[str, int]] = []
    result = {}
    closers = {"(": ")", "[": "]", "{": "}"}
    for i, token in enumerate(tokens):
        if token.text in closers:
            stack.append((closers[token.text], i))
        elif token.text in ("}", "]", ")"):
            if stack and stack[-1][0] == token.text:
                _, start = stack.pop()
                result[start] = i
                result[i] = start
            else:
                stack.clear()
    return result


def _class_headers(tokens: list[Token]):
    """Не стирает исходную базу при modded class Name без extends."""
    for i, token in enumerate(tokens[:-1]):
        if token.text != "class" or not IDENT_RE.fullmatch(tokens[i + 1].text):
            continue
        name = tokens[i + 1].text
        base = None
        j = i + 2
        angle = 0
        while j < len(tokens):
            text = tokens[j].text
            if text == "<":
                angle += 1
            elif text == ">":
                angle = max(0, angle - 1)
            elif not angle and text in ("extends", ":") and j + 1 < len(tokens):
                base = tokens[j + 1].text
            elif not angle and text in ("{", ";"):
                yield name, base, j if text == "{" else None, tokens[i + 1].line, i > 0 and tokens[i - 1].text == "modded"
                break
            j += 1


def _add_class(graph: dict[str, ClassInfo], name: str, base: str | None, declaration: Declaration, layer: str | None, *, modded=False, forward=False):
    info = graph.setdefault(name, ClassInfo())
    if declaration not in info.declarations:
        info.declarations.append(declaration)
    info.layers.add(layer)
    if base:
        info.bases.add(base)
    elif not modded and not forward:
        info.root = True


def load_hierarchy(type_db: str | Path):
    """Читает snapshot БД без изменения файла или создания SQLite-журналов."""
    db = Path(type_db).resolve(strict=True)
    wal = Path(str(db) + "-wal")
    if not db.is_file() or (wal.exists() and wal.stat().st_size):
        raise ValueError("Expected a completed type database snapshot without pending WAL")
    graph: dict[str, ClassInfo] = {}
    fields: dict[str, dict[str, list[Variable]]] = {}
    known_types = set(BUILTINS)
    with sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"classes", "files", "fields", "enums", "aliases"}.issubset(tables):
            raise ValueError("Type DB must contain classes/files/fields/enums/aliases tables")
        layers = dict(connection.execute("SELECT path, layer FROM files"))
        for name, base, modifiers, path, line, forward in connection.execute("SELECT name, base, modifiers, path, line, forward FROM classes"):
            owner_path = str(db.parent / "sources" / path) if not Path(path).is_absolute() else path
            _add_class(graph, name, base or None, Declaration(owner_path, int(line), "class"), layer_from_path(layers.get(path) or path), modded="modded" in (modifiers or "").split(), forward=bool(forward))
        known_types.update(graph)
        for table in ("enums", "aliases"):
            known_types.update(row[0] for row in connection.execute(f"SELECT name FROM {table}"))
        for owner, name, type_name, path, line in connection.execute("SELECT owner, name, type, path, line FROM fields"):
            owner_path = str(db.parent / "sources" / path) if not Path(path).is_absolute() else path
            variable = Variable(name, _type_root(type_name or ""), -1, Declaration(owner_path, int(line), "field"))
            fields.setdefault(owner, {}).setdefault(name, []).append(variable)
    return graph, fields, known_types


def _type_root(type_name: str) -> str:
    words = re.findall(r"[A-Za-z_]\w*", type_name)
    words = [word for word in words if word not in MODIFIERS]
    if "[" in type_name:
        return "array"
    return words[0] if words else ""


def _angle_end(tokens: list[Token], start: int) -> int | None:
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


def _initializer_end(tokens: list[Token], pairs: dict[int, int], start: int, known_types: set[str]) -> int:
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


def _variables(tokens: list[Token], pairs: dict[int, int], path: str, known_types: set[str]):
    excluded = set()
    for i, token in enumerate(tokens):
        if token.text == "typedef":
            j = i
            while j < len(tokens) and tokens[j].text != ";":
                excluded.add(j)
                j += 1
    seen = set()
    for i, token in enumerate(tokens):
        if i in excluded or (i and tokens[i - 1].text in (".", "::")):
            continue
        j = i
        while j < len(tokens) and tokens[j].text in MODIFIERS:
            j += 1
        if j >= len(tokens) or tokens[j].text not in known_types:
            continue
        type_name = tokens[j].text
        j += 1
        if j < len(tokens) and tokens[j].text == "<":
            j = _angle_end(tokens, j)
            if j is None:
                continue
        while j < len(tokens) and tokens[j].text == "[" and j in pairs:
            type_name = "array"
            j = pairs[j] + 1
        while j < len(tokens) and IDENT_RE.fullmatch(tokens[j].text):
            name = tokens[j]
            k = j + 1
            actual_type = type_name
            while k < len(tokens) and tokens[k].text == "[" and k in pairs:
                actual_type = "array"
                k = pairs[k] + 1
            if k >= len(tokens) or tokens[k].text not in ("=", ";", ",", ")", ":"):
                break
            if j not in seen:
                seen.add(j)
                yield Variable(name.text, actual_type, j, Declaration(path, name.line, "variable"))
            if tokens[k].text == "=":
                k = _initializer_end(tokens, pairs, k + 1, known_types)
            if k >= len(tokens) or tokens[k].text != ",":
                break
            j = k + 1


def build_model(source: str, path: str | Path, known_types: set[str]) -> SourceModel:
    path = str(path)
    tokens = tokenize(source)
    pairs = _pairs(tokens)
    scopes = [Scope(-1, len(tokens), "global")]
    class_open = {opening: name for name, _, opening, _, _ in _class_headers(tokens) if opening is not None}
    open_scope = {}
    for i, token in enumerate(tokens):
        if token.text == "{" and i in pairs:
            open_scope[i] = len(scopes)
            scopes.append(Scope(i, pairs[i], "class" if i in class_open else "block", class_open.get(i)))
        elif token.text in ("for", "foreach") and i + 1 in pairs and tokens[i + 1].text == "(":
            end = pairs[i + 1] + 1
            if end < len(tokens) and tokens[end].text == "{" and end in pairs:
                end = pairs[end]
            else:
                while end < len(tokens) and tokens[end].text != ";":
                    end += 1
            scopes.append(Scope(i + 1, end, "loop"))
    for i, scope in enumerate(scopes[1:], 1):
        containers = [j for j, parent in enumerate(scopes) if j != i and parent.start <= scope.start and parent.end >= scope.end and (parent.start < scope.start or parent.end > scope.end)]
        scope.parent = min(containers, key=lambda j: scopes[j].end - scopes[j].start) if containers else 0
    model = SourceModel(path, tokens, pairs, scopes)

    # Только заголовки функций, заканчивающиеся собственным телом; control/calls исключены.
    param_scopes = []
    controls = {"if", "for", "foreach", "while", "switch", "catch", "Cast"}
    for opening, close in pairs.items():
        if tokens[opening].text != "(" or opening == 0 or close + 1 not in open_scope:
            continue
        name = tokens[opening - 1].text
        before = tokens[opening - 2].text if opening > 1 else ""
        if not IDENT_RE.fullmatch(name) or name in controls or before in (".", "::", "=", "!", "&&", "||"):
            continue
        body_id = open_scope[close + 1]
        parent_id = scopes[body_id].parent
        if parent_id is not None and scopes[parent_id].kind not in ("class", "global"):
            continue
        scopes[body_id].kind = "method"
        param_scopes.append((opening, close, body_id))

    for variable in _variables(tokens, pairs, path, known_types):
        parameter = next((scope_id for start, end, scope_id in param_scopes if start < variable.token_index < end), None)
        scope_id = parameter if parameter is not None else model.scope_at(variable.token_index)
        scope = scopes[scope_id]
        kind = "parameter" if parameter is not None else "field" if scope.kind == "class" else "local"
        variable = Variable(variable.name, variable.type_name, scope.start if kind in ("parameter", "field") else variable.token_index, Declaration(path, variable.declaration.line, kind))
        scope.variables.setdefault(variable.name, []).append(variable)
    return model


def _chain(graph: dict[str, ClassInfo], name: str):
    chain = []
    while name not in chain:
        chain.append(name)
        info = graph.get(name)
        if info is None or len(info.bases) > 1:
            return chain, False
        if not info.bases:
            return chain, info.root
        name = next(iter(info.bases))
    return chain, False


def relation(graph: dict[str, ClassInfo], source_type: str, target_type: str) -> tuple[str, str | None]:
    """related, incompatible либо unresolved; общий предок не делает ветви совместимыми."""
    if source_type in BUILTINS or target_type in BUILTINS or source_type not in graph or target_type not in graph:
        return "unresolved", None
    if source_type == target_type:
        return "related", source_type
    source_chain, source_complete = _chain(graph, source_type)
    target_chain, target_complete = _chain(graph, target_type)
    if target_type in source_chain or source_type in target_chain:
        return "related", None
    common = next((name for name in source_chain if name in target_chain), None)
    if common is not None or (source_complete and target_complete):
        return "incompatible", common
    return "unresolved", None


def _field_variable(owner: str | None, name: str, graph, fields):
    if not owner:
        return None, "unknown_identifier"
    chain, _ = _chain(graph, owner)
    for class_name in chain:
        candidates = fields.get(class_name, {}).get(name, [])
        if candidates:
            types = {candidate.type_name for candidate in candidates}
            return (candidates[0], None) if len(types) == 1 else (None, "ambiguous_field")
    return None, "unknown_identifier"


def _resolve(model: SourceModel, index: int, name: str, graph, fields):
    scope_id: int | None = model.scope_at(index)
    while scope_id is not None:
        scope = model.scopes[scope_id]
        candidates = [variable for variable in scope.variables.get(name, []) if variable.token_index <= index]
        if candidates:
            return max(candidates, key=lambda variable: variable.token_index), None
        scope_id = scope.parent
    return _field_variable(model.owner_at(index), name, graph, fields)


def _available(info: ClassInfo, path: str) -> bool:
    layer = layer_from_path(path)
    return layer is not None and any(owner_layer is not None and int(owner_layer[0]) <= int(layer[0]) for owner_layer in info.layers)


def check_model(model: SourceModel, graph, fields) -> dict:
    counts = Counter(total=0, simple_identifier=0, related=0, incompatible=0, unresolved=0)
    reasons = Counter()
    findings = []
    unresolved_items = []
    tokens = model.tokens

    def unresolved(reason):
        counts["unresolved"] += 1
        reasons[reason] += 1
        unresolved_items.append({"path": model.path, "line": tokens[i].line, "reason": reason, "target_type": tokens[i].text})

    for i in range(len(tokens) - 3):
        if tokens[i + 1].text != "." or tokens[i + 2].text != "Cast" or tokens[i + 3].text != "(":
            continue
        counts["total"] += 1
        if not IDENT_RE.fullmatch(tokens[i].text) or (i and tokens[i - 1].text in (".", "::")):
            unresolved("complex_target")
            continue
        end = model.pairs.get(i + 3)
        if end != i + 5 or not IDENT_RE.fullmatch(tokens[i + 4].text):
            unresolved("complex_argument")
            continue
        counts["simple_identifier"] += 1
        target = tokens[i].text
        argument = tokens[i + 4].text
        if target in BUILTINS or target not in graph:
            unresolved("target_not_known_class")
            continue
        if not _available(graph[target], model.path):
            unresolved("target_layer_unknown_or_unavailable")
            continue
        variable, reason = _resolve(model, i + 4, argument, graph, fields)
        if variable is None:
            unresolved(reason)
            continue
        if variable.type_name in BUILTINS or variable.type_name not in graph:
            unresolved("source_not_known_class")
            continue
        if not _available(graph[variable.type_name], model.path):
            unresolved("source_layer_unknown_or_unavailable")
            continue
        result, common = relation(graph, variable.type_name, target)
        if result == "unresolved":
            unresolved("incomplete_or_ambiguous_hierarchy")
        elif result == "related":
            counts["related"] += 1
        else:
            counts["incompatible"] += 1
            findings.append({
                "path": model.path,
                "line": tokens[i].line,
                "source_name": argument,
                "source_type": variable.type_name,
                "source_decl": asdict(variable.declaration),
                "target_type": target,
                "target_decl": asdict(graph[target].declarations[0]),
                "common_ancestor": common,
            })
    return {"summary": dict(counts), "unresolved_reasons": dict(reasons), "unresolved": unresolved_items, "findings": findings}


def scan_cast_relations(
    paths: str | Path | Iterable[str | Path],
    *,
    type_db: str | Path,
    types_from: str | Path | Iterable[str | Path] = (),
) -> dict:
    graph, fields, known_types = load_hierarchy(type_db)
    checked = source_files(paths)
    if not checked:
        raise ValueError("No .c sources selected for checking")
    own_files = source_files(types_from)
    cache = {path: path.read_text(encoding="utf-8-sig") for path in set(checked) | set(own_files)}
    for path in own_files:
        for name, base, opening, line, modded in _class_headers(tokenize(cache[path])):
            _add_class(graph, name, base, Declaration(str(path), line, "class"), layer_from_path(path), modded=modded, forward=opening is None)
    known_types.update(graph)
    # Поля полного source нужны для modded определений и наследуемых полей из других файлов.
    models = {path: build_model(cache[path], path, known_types) for path in set(checked) | set(own_files)}
    for path in own_files:
        for scope in models[path].scopes:
            if scope.kind == "class" and scope.owner:
                for name, variables in scope.variables.items():
                    fields.setdefault(scope.owner, {}).setdefault(name, []).extend(variables)
    counts = Counter(total=0, simple_identifier=0, related=0, incompatible=0, unresolved=0)
    reasons = Counter()
    findings = []
    unresolved_items = []
    for path in checked:
        report = check_model(models[path], graph, fields)
        counts.update(report["summary"])
        reasons.update(report["unresolved_reasons"])
        findings.extend(report["findings"])
        unresolved_items.extend(report["unresolved"])
    return {
        "schema_version": 1,
        "check": "enforce_cast_relations",
        "status": "conflict" if counts["incompatible"] else "pass",
        "files_checked": len(checked),
        "own_type_files": len(own_files),
        "summary": dict(counts),
        "unresolved_reasons": dict(reasons),
        "unresolved": unresolved_items,
        "findings": sorted(findings, key=lambda finding: (finding["path"].casefold(), finding["line"], finding["source_name"])),
        "limitations": LIMITATIONS,
    }


def self_test():
    graph = {}
    for name, base in (("Object", None), ("EntityAI", "Object"), ("Building", "EntityAI"), ("BuildingBase", "Building"), ("InventoryItem", "EntityAI"), ("ItemBase", "InventoryItem"), ("BaseBuildingBase", "ItemBase"), ("Example", "Object")):
        _add_class(graph, name, base, Declaration("3_Game/Types.c", 1, "class"), "3_Game")
    # В настоящей БД primitives тоже описаны class-декларациями; это не объектные типы.
    _add_class(graph, "int", None, Declaration("1_Core/Primitives.c", 1, "class"), "1_Core")
    known = set(graph) | set(BUILTINS)
    cases = [
        ("building_disjoint", "void F(Building x) { BaseBuildingBase.Cast(x); }", 1, 0, 0),
        ("building_related_downcast", "void F(Building x) { BuildingBase.Cast(x); }", 0, 1, 0),
        ("object_downcast", "void F(Object x) { BaseBuildingBase.Cast(x); }", 0, 1, 0),
        ("itembase_downcast", "void F(ItemBase x) { BaseBuildingBase.Cast(x); }", 0, 1, 0),
        ("local_shadows_parameter", "void F(Building x) { { Object x; BaseBuildingBase.Cast(x); } BaseBuildingBase.Cast(x); }", 1, 1, 0),
        ("parameter_shadows_field", "class Example { Building x; void F(Object x) { BaseBuildingBase.Cast(x); } void G() { BaseBuildingBase.Cast(x); } }", 1, 1, 0),
        ("field_after_method", "class Example { void F() { BaseBuildingBase.Cast(x); } Building x; }", 1, 0, 0),
        ("loop_scope", "class Example { Building x; void F() { foreach (Object x : values) { BaseBuildingBase.Cast(x); } BaseBuildingBase.Cast(x); } }", 1, 1, 0),
        ("for_scope", "class Example { Building x; void F() { for (Object x = GetObject(); ready; next) { BaseBuildingBase.Cast(x); } BaseBuildingBase.Cast(x); } }", 1, 1, 0),
        ("declaration_order", "class Example { Building x; void F() { { BaseBuildingBase.Cast(x); Object x; BaseBuildingBase.Cast(x); } } }", 1, 1, 0),
        ("comments_strings", '// BaseBuildingBase.Cast(x);\nstring s = "BaseBuildingBase.Cast(x);"; /* BaseBuildingBase.Cast(x); */', 0, 0, 0),
        ("directives", "#define F BaseBuildingBase.Cast(x); \\\nBuildingBase.Cast(x);\n", 0, 0, 0),
        ("complex_unknown_primitive", "void F(int x) { BaseBuildingBase.Cast(x); BaseBuildingBase.Cast(GetObject()); BaseBuildingBase.Cast(missing); }", 0, 0, 3),
        ("initialized_comma", "void F() { Building safe = GetBuilding(), x = GetBuilding(); BaseBuildingBase.Cast(x); }", 1, 0, 0),
    ]
    passed = []
    for name, source, incompatible, related, unresolved in cases:
        report = check_model(build_model(source, "4_World/Test.c", known), graph, {})
        expected = (incompatible, related, unresolved)
        actual = tuple(report["summary"][key] for key in ("incompatible", "related", "unresolved"))
        assert actual == expected, f"{name}: expected {expected}, got {actual}"
        passed.append(name)
    for name, base, opening, line, modded in _class_headers(tokenize("modded class Building {}")):
        _add_class(graph, name, base, Declaration("4_World/Mod.c", line, "class"), "4_World", modded=modded, forward=opening is None)
    assert relation(graph, "Building", "BaseBuildingBase")[0] == "incompatible"
    assert graph["Building"].bases == {"EntityAI"}
    passed.append("modded_preserves_base")
    fields = {"Object": {"inherited": [Variable("inherited", "Building", -1, Declaration("3_Game/Types.c", 1, "field"))]}}
    report = check_model(build_model("class Example { void F() { BaseBuildingBase.Cast(inherited); } }", "4_World/Test.c", known), graph, fields)
    assert report["summary"]["incompatible"] == 1
    passed.append("inherited_field")
    return {"status": "pass", "tests_passed": len(passed), "tests": passed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type-db", type=Path)
    parser.add_argument("--types-from", action="append", type=Path, default=[])
    parser.add_argument("--strict", action="store_true", help="Exit 1 only for proven incompatible casts")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            report = self_test()
        else:
            if args.type_db is None or not args.paths:
                parser.error("--type-db and at least one source path are required")
            report = scan_cast_relations(args.paths, type_db=args.type_db, types_from=args.types_from)
    except (OSError, ValueError, sqlite3.Error, AssertionError) as error:
        print(json.dumps({"check": "enforce_cast_relations", "status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and report["status"] == "conflict" else 0


if __name__ == "__main__":
    sys.exit(main())
