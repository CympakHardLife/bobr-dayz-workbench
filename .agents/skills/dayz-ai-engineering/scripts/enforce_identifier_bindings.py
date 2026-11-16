#!/usr/bin/env python3
"""Адресный read-only review необъявленных generated identifiers zn5d_mNNNN.

API: scan_identifier_bindings(paths, *, type_db=None, types_from=()) -> dict.
CLI: python enforce_identifier_bindings.py --type-db symbols.sqlite \
    --types-from FULL_SOURCE SOURCE_OR_OVERLAY --strict

Нужны соседние enforce_cast_relations.py, enforce_local_declarations.py и
enforce_name_collisions.py. Они используют стандартную библиотеку Python.
Это проверка простых bindings внутри методов, а не компилятор Enforce.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sqlite3
import sys

from enforce_cast_relations import (
    Declaration, Variable, _add_class, _class_headers, _resolve,
    build_model, layer_from_path, load_hierarchy, source_files,
)
from enforce_local_declarations import BUILTINS, declarations, method_ranges, token_pairs
from enforce_name_collisions import source_type_declarations


GENERATED = re.compile(r"zn5d_m[0-9]+\Z")
LIMITATIONS = [
    "Only simple identifiers matching zn5d_m[0-9]+ inside methods are checked.",
    "Bindings use block/loop visibility; declarations in different methods do not bind the use.",
    "Method/type declarations and member RHS names are excluded; calls and named method references are not treated as unknown locals.",
    "Ambiguous method callbacks, macros, unsupported declarations and default-argument contexts remain unresolved.",
    "Preprocessor branches, dynamic invocation and actual modded load order are not compiled or simulated.",
    "A clean report is not proof of Enforce compilation or runtime correctness.",
]


def _read_api_symbols(type_db):
    methods = defaultdict(set)
    constants = set()
    if type_db is None:
        return methods, constants
    db = Path(type_db).resolve(strict=True)
    # load_hierarchy уже проверяет завершённость snapshot и отсутствие pending WAL.
    with sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        for owner, name in connection.execute("SELECT owner,name FROM methods"):
            methods[owner].add(name)
        for values_json, in connection.execute("SELECT values_json FROM enums"):
            constants.update(json.loads(values_json or "[]"))
    return methods, constants


def prepare_context(texts, type_db=None):
    """Принимает {path: text}; регрессионные примеры можно проверить целиком в памяти."""
    if type_db is None:
        graph, fields, known_types = {}, {}, set(BUILTINS)
    else:
        graph, fields, known_types = load_hierarchy(type_db)
        known_types.update(BUILTINS)
    methods, constants = _read_api_symbols(type_db)
    for path, text in texts.items():
        for name, declaration in source_type_declarations(text, path):
            known_types.add(name)
    models = {path: build_model(text, path, known_types) for path, text in texts.items()}
    for path, model in models.items():
        token_pairs(model.tokens, str(path))
    for path, model in models.items():
        for name, base, opening, line, modded in _class_headers(model.tokens):
            _add_class(graph, name, base, Declaration(str(path), line, "class"), layer_from_path(path), modded=modded, forward=opening is None)
    global_variables = defaultdict(list)
    all_declarations = defaultdict(list)
    metadata = {}
    for path, model in models.items():
        ranges = method_ranges(model.tokens, model.pairs)
        values, unknown = declarations(model.tokens, model.pairs, known_types)
        positions = {value["token_index"] for value in values}
        method_positions = {method.opening - 1 for method in ranges}
        # Убираем guessed prototype parameters из class scope и заново назначаем
        # точные declaration tokens полям, параметрам и блочным локалам.
        for scope in model.scopes:
            scope.variables.clear()
        for method in ranges:
            methods[method.owner].add(method.name)
        for value in values:
            index = value["token_index"]
            method = next((method for method in ranges if method.opening < index < method.end), None)
            if method is not None and index < method.closing:
                kind = "parameter"
                if method.body is None:
                    continue
                scope_id = next((i for i, scope in enumerate(model.scopes) if scope.start == method.body), None)
                if scope_id is None:
                    continue
                visible = model.scopes[scope_id].start
            else:
                scope_id = model.scope_at(index)
                scope = model.scopes[scope_id]
                kind = "field" if scope.kind == "class" else "global" if scope.kind == "global" else "local"
                visible = scope.start if kind == "field" else index
            variable = Variable(value["name"], value["type"], visible, Declaration(str(path), value["line"], kind))
            scope = model.scopes[scope_id]
            scope.variables.setdefault(variable.name, []).append(variable)
            all_declarations[variable.name].append({"path": str(path), "line": value["line"], "method": method.name if method else None, "kind": kind})
            if kind == "field" and scope.owner:
                fields.setdefault(scope.owner, {}).setdefault(variable.name, []).append(variable)
            elif kind == "global":
                global_variables[variable.name].append((str(path), variable))
        for i, token in enumerate(model.tokens):
            if token.text == "enum":
                opening = i + 1
                while opening < len(model.tokens) and model.tokens[opening].text not in ("{", ";"):
                    opening += 1
                if opening in model.pairs and model.tokens[opening].text == "{":
                    end = model.pairs[opening]
                    for j in range(opening + 1, end):
                        if model.tokens[j - 1].text in ("{", ",") and GENERATED.fullmatch(model.tokens[j].text):
                            constants.add(model.tokens[j].text)
        macros = set(re.findall(r"(?m)^\s*#\s*define\s+(zn5d_m[0-9]+)\b", texts[path]))
        metadata[path] = {"ranges": ranges, "declarations": positions, "method_declarations": method_positions, "unparsed": unknown, "macros": macros}
    return {"models": models, "metadata": metadata, "graph": graph, "fields": fields, "known_types": known_types, "methods": methods, "method_names": set().union(*methods.values()) if methods else set(), "constants": constants, "globals": global_variables, "all_declarations": all_declarations}


def check_model(path, context):
    model = context["models"][path]
    meta = context["metadata"][path]
    counts = Counter(generated_tokens=0, declaration_tokens=0, bound_uses=0, member_names=0, method_declarations=0, named_calls=0, type_symbols=0, outside_methods=0, unresolved=0, unbound=0)
    findings = []
    unresolved = []

    def defer(token, method, reason):
        counts["unresolved"] += 1
        unresolved.append({"path": str(path), "line": token.line, "name": token.text, "method": method.name if method else None, "reason": reason})

    for index, token in enumerate(model.tokens):
        name = token.text
        if not GENERATED.fullmatch(name):
            continue
        counts["generated_tokens"] += 1
        if index in meta["declarations"]:
            counts["declaration_tokens"] += 1
            continue
        if index in meta["method_declarations"]:
            counts["method_declarations"] += 1
            continue
        if index and model.tokens[index - 1].text in (".", "::"):
            counts["member_names"] += 1
            continue
        if name in context["known_types"]:
            counts["type_symbols"] += 1
            continue
        method = next((method for method in meta["ranges"] if method.opening < index < method.end), None)
        if method is None:
            counts["outside_methods"] += 1
            continue
        if index < method.closing:
            defer(token, method, "default_argument_context")
            continue
        variable, _ = _resolve(model, index, name, context["graph"], context["fields"])
        if variable is not None or name in context["constants"]:
            counts["bound_uses"] += 1
            continue
        globals_ = context["globals"].get(name, [])
        if globals_:
            layer = layer_from_path(path)
            if layer and any(layer_from_path(owner_path) and int(layer_from_path(owner_path)[0]) <= int(layer[0]) for owner_path, _ in globals_):
                counts["bound_uses"] += 1
            else:
                defer(token, method, "global_layer_not_resolved")
            continue
        if index + 1 < len(model.tokens) and model.tokens[index + 1].text == "(":
            counts["named_calls"] += 1
            if name not in context["method_names"]:
                defer(token, method, "unknown_named_call")
            continue
        if name in context["method_names"]:
            defer(token, method, "named_method_reference_or_callback")
            continue
        if name in meta["macros"]:
            defer(token, method, "macro_symbol")
            continue
        if any(method.opening < item["token_index"] < method.end for item in meta["unparsed"]):
            defer(token, method, "declaration_shape_unresolved")
            continue
        counts["unbound"] += 1
        findings.append({"path": str(path), "line": token.line, "name": name, "owner": method.owner, "method": method.name, "reason": "not_visible_local_parameter_or_field", "declarations_elsewhere": context["all_declarations"].get(name, [])[:8]})
    return {"counts": dict(counts), "findings": findings, "unresolved": unresolved}


def scan_identifier_bindings(paths, *, type_db=None, types_from=()):
    checked = source_files(paths)
    if not checked:
        raise ValueError("No .c sources selected for checking")
    own_files = source_files(types_from)
    texts = {path: path.read_text(encoding="utf-8-sig") for path in set(checked) | set(own_files)}
    context = prepare_context(texts, type_db)
    counts = Counter()
    findings = []
    unresolved = []
    for path in checked:
        report = check_model(path, context)
        counts.update(report["counts"])
        findings.extend(report["findings"])
        unresolved.extend(report["unresolved"])
    return {"schema_version": 1, "check": "enforce_identifier_bindings", "status": "review" if findings else "pass", "files_checked": len(checked), "own_type_files": len(own_files), "counts": dict(counts), "findings": sorted(findings, key=lambda finding: (finding["path"].casefold(), finding["line"], finding["name"])), "unresolved": unresolved, "limitations": LIMITATIONS}


def self_test():
    failed = "class C { void Other() { vector zn5d_m2972; } void Launch() { vector zn5d_m2973; column.Use(zn5d_m2972); } }"
    fixed = failed.replace("column.Use(zn5d_m2972)", "column.Use(order.m_zn5d_f2934)")
    cases = [
        ("actual_stale_local", failed, ["zn5d_m2972"]),
        ("actual_fixed_entity_field", fixed, []),
        ("declared_other_method", "void A() { int zn5d_m12; } void B() { Use(zn5d_m12); }", ["zn5d_m12"]),
        ("parameter", "void A(vector zn5d_m12) { Use(zn5d_m12); }", []),
        ("foreach", "void A() { foreach (Object zn5d_m12 : values) { Use(zn5d_m12); } }", []),
        ("for_loop_visibility", "void A() { for (int zn5d_m12 = 0; zn5d_m12 < 2; zn5d_m12++) { Use(zn5d_m12); } Use(zn5d_m12); }", ["zn5d_m12"]),
        ("generics", "void A() { array<ref Object> zn5d_m12; Use(zn5d_m12); }", []),
        ("block_visibility", "void A() { { int zn5d_m12; } Use(zn5d_m12); }", ["zn5d_m12"]),
        ("sibling_visibility", "void A() { if (ready) { int zn5d_m12; } else { Use(zn5d_m12); } }", ["zn5d_m12"]),
        ("method_names", "class C { void zn5d_m12() {} void A() { zn5d_m12(); queue.CallLater(zn5d_m12); } }", []),
        ("callback_named_elsewhere", "class A { void zn5d_m12() {} } class B { void F() { queue.CallLater(zn5d_m12); } }", []),
        ("member_names", "void A(Object item) { item.zn5d_m12(); item.zn5d_m13 = 1; }", []),
        ("field", "class C { int zn5d_m12; void A() { Use(zn5d_m12); } }", []),
        ("inherited_field", "class Base { int zn5d_m12; } class C extends Base { void A() { Use(zn5d_m12); } }", []),
        ("comments_strings", 'void A() { string s = "zn5d_m12"; /* zn5d_m12 */ // zn5d_m12\n }', []),
        ("type_name", "class zn5d_m12 {} void A() { zn5d_m12 value; }", []),
        ("prototype_param_not_field", "class C { proto void B(int zn5d_m12); void A() { Use(zn5d_m12); } }", ["zn5d_m12"]),
        ("forward_local_not_visible", "void A() { Use(zn5d_m12); int zn5d_m12; }", ["zn5d_m12"]),
        ("non_target_unknown_not_checked", "void A() { Use(otherUnknownIdentifier); }", []),
    ]
    passed = []
    for label, source, expected in cases:
        path = "4_World/Test.c"
        context = prepare_context({path: source})
        report = check_model(path, context)
        assert [finding["name"] for finding in report["findings"]] == expected, (label, report)
        if label == "callback_named_elsewhere":
            assert any(item["reason"] == "named_method_reference_or_callback" for item in report["unresolved"])
        passed.append(label)
    try:
        prepare_context({"4_World/Test.c": "void A() { Use(zn5d_m12);"})
    except ValueError:
        passed.append("unbalanced_input_error")
    else:
        raise AssertionError("unbalanced_input_error")
    try:
        scan_identifier_bindings([])
    except ValueError:
        passed.append("empty_input_error")
    else:
        raise AssertionError("empty_input_error")
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
            report = scan_identifier_bindings(args.paths, type_db=args.type_db, types_from=args.types_from)
    except (OSError, ValueError, sqlite3.Error, AssertionError) as error:
        print(json.dumps({"check": "enforce_identifier_bindings", "status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and report["status"] == "review" else 0


if __name__ == "__main__":
    sys.exit(main())
