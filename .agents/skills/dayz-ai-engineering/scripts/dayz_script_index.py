"""Снимок доступного Enforce API из scripts.pbo и адресная проверка своего мода.

build --pbo PATH --out DIR; query --db PATH --name SYMBOL;
check --db PATH --source DIR --out DIR.
Не компилятор Enforce: препроцессор не исполняется, типы выражений не выводятся.
"""
import argparse
import bisect
import collections
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import struct

LEX = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27', re.S)
MODIFIERS = "static proto native override protected private public external volatile const owned ref event sealed final autoptr notnull"
MOD_RX = r"(?:(?:" + "|".join(MODIFIERS.split()) + r")\s+)*"
TYPE_RX = r"[A-Za-z_]\w*(?:\s*<[^;{}()]*?>)?(?:\s*\[[^\]\n]*\])?"
METHOD = re.compile(r"(?m)^[ \t]*(?P<mods>" + MOD_RX + r")(?P<ret>" + TYPE_RX + r")\s+(?P<name>~?[A-Za-z_]\w*)\s*\(")
CLASS = re.compile(r"\b(?P<mods>(?:(?:modded|native|sealed)\s+)*)class\s+(?P<name>[A-Za-z_]\w*)(?:\s*<[^;{}]*?>)?(?:\s*(?::|extends)\s*(?P<base>[A-Za-z_]\w*)(?:\s*<[^;{}]*?>)?)?\s*(?P<tail>[;{])")
FIELD = re.compile(r"(?m)^[ \t]*(?P<mods>" + MOD_RX + r")(?P<type>" + TYPE_RX + r")\s+(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]\n]*\])?\s*(?:=|;)")
KEYWORDS = set("if else while for foreach switch return new delete super this class enum typedef case break continue do true false null".split())


def pbo_files(path):
    data = path.read_bytes()
    offset = 0
    def z():
        nonlocal offset
        end = data.index(b"\0", offset)
        value = data[offset:end].decode("utf-8")
        offset = end + 1
        return value
    entries = []
    properties = {}
    while True:
        name = z()
        packing, original, reserved, timestamp, size = struct.unpack_from("<5I", data, offset)
        offset += 20
        if not name and packing == 0x56657273:
            while True:
                key = z()
                if not key:
                    break
                properties[key] = z()
            continue
        if not name and not packing and not size:
            break
        if packing:
            raise ValueError("Сжатый PBO не поддерживается; защита не обходится")
        entries.append((name.replace("\\", "/"), size))
    files = {}
    for name, size in entries:
        if offset + size > len(data):
            raise ValueError("Неполный PBO")
        files[name] = data[offset:offset + size]
        offset += size
    return properties, files


def masked(text):
    code = LEX.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)
    return re.sub(r"(?m)^[ \t]*#[^\n]*", lambda m: " " * len(m.group()), code)


def norm(s):
    return " ".join(s.split())


def split_args(text):
    code = masked(text)
    depth = 0
    begin = 0
    result = []
    for i, c in enumerate(code):
        if c in "(<[{":
            depth += 1
        elif c in ")>]}":
            depth -= 1
        elif c == "," and depth == 0:
            result.append(text[begin:i].strip())
            begin = i + 1
    if text[begin:].strip():
        result.append(text[begin:].strip())
    return result


def parameters(text):
    result = []
    for arg in split_args(text):
        declaration = arg.split("=", 1)[0].strip()
        match = re.match(r"(?P<type>.*?)\b(?P<name>\w+)\s*(?P<array>\[[^\]]*\])?\s*$", declaration, re.S)
        result.append({"declaration": norm(arg), "type": norm(match["type"]) if match else "?", "name": match["name"] if match else "?", "optional": "=" in arg})
    return result


def parse(text, path):
    code = masked(text)
    offsets = [0] + [m.end() for m in re.finditer("\n", text)]
    line = lambda pos: bisect.bisect_right(offsets, pos)
    levels = []
    depth = 0
    pairs = {}
    stack = []
    errors = []
    for i, c in enumerate(code):
        levels.append(depth)
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c in "({[":
            stack.append((c, i))
        elif c in ")}]":
            if stack and stack[-1][0] == {")": "(", "}": "{", "]": "["}[c]:
                _, start = stack.pop()
                pairs[start] = i
            else:
                errors.append({"line": line(i), "message": "unmatched " + c})
    errors.extend({"line": line(i), "message": "unclosed " + c} for c, i in stack)
    classes = []
    for match in CLASS.finditer(code):
        start = match.end() - 1
        if match["tail"] == ";":
            end = start
        else:
            end = pairs.get(start, len(code) - 1)
        classes.append({"name": match["name"], "base": match["base"] or "", "mods": norm(match["mods"]), "line": line(match.start()), "start": start, "end": end, "depth": levels[start] + 1, "forward": match["tail"] == ";"})
    def owner_at(pos):
        found = [c for c in classes if c["start"] < pos < c["end"]]
        return max(found, key=lambda c: c["start"]) if found else None
    methods = []
    for match in METHOD.finditer(code):
        opening = match.end() - 1
        closing = pairs.get(opening)
        if closing is None:
            continue
        owner = owner_at(match.start())
        if levels[match.start()] != (owner["depth"] if owner else 0):
            continue
        tail = closing + 1
        while tail < len(code) and code[tail].isspace():
            tail += 1
        if tail >= len(code) or code[tail] not in ";{":
            continue
        if match["ret"] in KEYWORDS or match["name"] in KEYWORDS:
            continue
        end = pairs.get(tail, tail) if code[tail] == "{" else tail
        args = text[opening + 1:closing]
        methods.append({"owner": owner["name"] if owner else "", "name": match["name"], "returns": norm(match["ret"]), "mods": norm(match["mods"]), "params": parameters(args), "signature": norm(text[match.start():closing + 1]), "line": line(match.start()), "start": match.start(), "body_start": tail if code[tail] == "{" else -1, "end": end, "class_start": owner["start"] if owner else -1})
    fields = []
    for match in FIELD.finditer(code):
        owner = owner_at(match.start())
        if not owner or levels[match.start()] != owner["depth"]:
            continue
        if match["type"] in KEYWORDS:
            continue
        fields.append({"owner": owner["name"], "name": match["name"], "type": norm(match["type"]), "mods": norm(match["mods"]), "line": line(match.start())})
    enums = []
    for match in re.finditer(r"\benum\s+(\w+)(?:\s*:\s*\w+)?\s*\{", code):
        opening = match.end() - 1
        closing = pairs.get(opening, opening)
        values = re.findall(r"(?:^|,)\s*(\w+)", code[opening + 1:closing])
        enums.append({"name": match[1], "values": values, "line": line(match.start())})
    aliases = [{"name": m[2], "type": norm(m[1]), "line": line(m.start())} for m in re.finditer(r"\btypedef\s+([^;{}]+?)\s+(\w+)\s*;", code)]
    return {"path": path, "classes": classes, "methods": methods, "fields": fields, "enums": enums, "aliases": aliases, "balance_notes": errors}


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def build(pbo, out, resume_incomplete=False):
    if out.exists():
        allowed = {"sources", "sources_raw", "symbols.sqlite", "symbols.sqlite-journal", ".building"}
        if not resume_incomplete or (out / "manifest.json").exists() or not {p.name for p in out.iterdir()} <= allowed:
            raise ValueError("Папка снимка уже существует: готовую базу не перезаписываем")
    properties, packed = pbo_files(pbo)
    out.mkdir(parents=True, exist_ok=resume_incomplete)
    (out / ".building").write_text("Incomplete generated snapshot", "utf-8")
    db = sqlite3.connect(out / "symbols.sqlite")
    db.executescript("""
        DROP TABLE IF EXISTS files;
        DROP TABLE IF EXISTS classes;
        DROP TABLE IF EXISTS methods;
        DROP TABLE IF EXISTS fields;
        DROP TABLE IF EXISTS enums;
        DROP TABLE IF EXISTS metadata;
        DROP TABLE IF EXISTS aliases;
        CREATE TABLE files(path TEXT PRIMARY KEY, layer TEXT, sha256 TEXT, lines INTEGER);
        CREATE TABLE classes(name TEXT, base TEXT, modifiers TEXT, path TEXT, line INTEGER, forward INTEGER);
        CREATE TABLE methods(owner TEXT, name TEXT, returns_type TEXT, modifiers TEXT, parameters TEXT, signature TEXT, path TEXT, line INTEGER);
        CREATE TABLE fields(owner TEXT, name TEXT, type TEXT, modifiers TEXT, path TEXT, line INTEGER);
        CREATE TABLE enums(name TEXT, values_json TEXT, path TEXT, line INTEGER);
        CREATE TABLE aliases(name TEXT, type TEXT, path TEXT, line INTEGER);
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX ix_classes_name ON classes(name);
        CREATE INDEX ix_methods_owner_name ON methods(owner,name);
        CREATE INDEX ix_methods_name ON methods(name);
        CREATE INDEX ix_fields_owner_name ON fields(owner,name);
    """)
    counts = collections.Counter()
    balance = []
    classes_tsv = ["class\tbase\tmodifiers\tlayer\tfile\tline"]
    methods_tsv = ["class\tmethod\tsignature\tlayer\tfile\tline"]
    manifest_files = []
    encodings = []
    for name, raw in sorted(packed.items()):
        if not name.lower().endswith(".c"):
            continue
        target = (out / "sources" / name).resolve()
        if not target.is_relative_to((out / "sources").resolve()):
            raise ValueError("Некорректный путь PBO: " + name)
        encoding = "utf-8-sig"
        try:
            text = raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            encoding = "cp1252-with-replacement"
            text = raw.decode("cp1252", errors="replace")
            encodings.append({"path": name, "decoding": encoding, "replacement_characters": text.count(chr(0xfffd))})
        text = text.replace("\r\r\n", "\n").replace("\r\n", "\n")
        original = out / "sources_raw" / name
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(raw)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
        record = parse(text, name)
        layer = name.split("/")[0]
        digest = hashlib.sha256(raw).hexdigest()
        db.execute("INSERT INTO files VALUES(?,?,?,?)", (name, layer, digest, len(text.splitlines())))
        manifest_files.append({"path": name, "sha256_original": digest, "lines": len(text.splitlines()), "decoding": encoding})
        counts["files"] += 1
        for c in record["classes"]:
            db.execute("INSERT INTO classes VALUES(?,?,?,?,?,?)", (c["name"], c["base"], c["mods"], name, c["line"], c["forward"]))
            classes_tsv.append("\t".join(str(v) for v in (c["name"], c["base"], c["mods"], layer, name, c["line"])))
        for m in record["methods"]:
            db.execute("INSERT INTO methods VALUES(?,?,?,?,?,?,?,?)", (m["owner"], m["name"], m["returns"], m["mods"], json.dumps(m["params"], ensure_ascii=False), m["signature"], name, m["line"]))
            methods_tsv.append("\t".join(str(v) for v in (m["owner"], m["name"], m["signature"], layer, name, m["line"])))
        for f in record["fields"]:
            db.execute("INSERT INTO fields VALUES(?,?,?,?,?,?)", (f["owner"], f["name"], f["type"], f["mods"], name, f["line"]))
        for e in record["enums"]:
            db.execute("INSERT INTO enums VALUES(?,?,?,?)", (e["name"], json.dumps(e["values"]), name, e["line"]))
        for a in record["aliases"]:
            db.execute("INSERT INTO aliases VALUES(?,?,?,?)", (a["name"], a["type"], name, a["line"]))
        for kind in ("classes", "methods", "fields", "enums", "aliases"):
            counts[kind] += len(record[kind])
        if record["balance_notes"]:
            balance.append({"path": name, "notes": record["balance_notes"]})
    metadata = {"source_pbo": str(pbo), "source_sha256": hashlib.sha256(pbo.read_bytes()).hexdigest(), "created_utc": datetime.now(timezone.utc).isoformat(), "pbo_properties": properties, "counts": dict(counts), "game_version": "not inferred", "conditional_branches": "union, not preprocessed", "parser": "lexical source index, not Enforce compiler", "balance_notes_files": len(balance)}
    for key, value in metadata.items():
        db.execute("INSERT INTO metadata VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))
    db.commit()
    db.close()
    dump(out / "manifest.json", {**metadata, "files": manifest_files})
    dump(out / "parser_notes.json", balance)
    dump(out / "encoding_notes.json", encodings)
    (out / "CLASSES.tsv").write_text("\n".join(classes_tsv) + "\n", "utf-8")
    (out / "METHODS.tsv").write_text("\n".join(methods_tsv) + "\n", "utf-8")
    shutil.copy2(Path(__file__), out / "dayz_script_index.py")
    (out / "README.md").write_text("""# Справочник фактически установленных скриптов DayZ

Источник: локальный DayZ/dta/scripts.pbo. SHA256 и время чтения — manifest.json. Версия игры из имени старой базы не выводится. Предыдущая KnowledgeBase сохранена.

- symbols.sqlite — классы, родители, методы с сигнатурами/модификаторами, поля, enum, слой и файл:строка.
- CLASSES.tsv и METHODS.tsv — быстрый поиск через rg.
- sources/ — полный доступный скриптовый исходник с нормализованными переносами строк; исходные хеши каждого файла в манифесте.
- sources_raw/ — побайтные оригиналы. encoding_notes.json перечисляет файлы с не-UTF8 комментариями, для нормализованного текста которых использована cp1252.
- dayz_script_index.py — самостоятельный инструмент build/query/check на стандартной библиотеке Python; без установки пакетов.

Запрос: `python dayz_script_index.py query --db symbols.sqlite --name Bone`. Для метода укажи `--name GetBonePositionWS`. Проверка своего исходника: `python dayz_script_index.py check --db symbols.sqlite --source ПУТЬ --out НОВАЯ_ПАПКА`.

Перед изменением кода: найти класс/метод, прочитать исходное объявление и родителей; проверить параметры, access, модуль и фактический контекст вызова. Client/Server определяется ветками IsServer/IsClient/ifdef и местом вызова, а не только папкой модуля. Отсутствие в индексе само по себе не доказывает отсутствие API.

Ограничения: это статический лексический индекс, не компилятор Enforce и не исходники C++ движка. Условные ветки представлены вместе, препроцессор не исполняется; перегрузки и дубликаты сохранены. parser_notes.json перечисляет файлы с несовпадающими скобками в объединении веток. Макросы и необычные объявления могут требовать ручной проверки. Сигнатуры не доказывают реальное поведение и стоимость native вызова. Проверка check ловит конфликты имён классов/методов, подозрительные override и баланс скобок; не доказывает правильность всех выражений или компиляцию мода.
""", "utf-8")
    (out / ".building").unlink()
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


def read_db(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def query(path, name):
    db = read_db(path)
    result = {}
    for table, sql in (("classes", "name=?"), ("methods", "name=? OR owner=?"), ("fields", "name=? OR owner=?"), ("enums", "name=?"), ("aliases", "name=?")):
        result[table] = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {sql}", (name, name) if "OR" in sql else (name,))]
    result["ancestors"] = []
    owner = name
    seen = set()
    while owner and owner not in seen:
        seen.add(owner)
        parent = db.execute("SELECT base FROM classes WHERE name=? AND base!='' LIMIT 1", (owner,)).fetchone()
        if not parent:
            break
        owner = parent[0]
        result["ancestors"].append({"class": owner, "methods": [dict(r) for r in db.execute("SELECT * FROM methods WHERE owner=?", (owner,))]})
    db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


ACCESS_LIMITATIONS = [
    "Direct simple receiver.method calls only; chains/returned receivers and field reads/writes are not checked.",
    "Overload selection uses arity/signature declarations, not argument-expression types.",
    "Preprocessor branches, actual modded load order and engine-specific receiver restrictions require source/compiler review.",
    "Static-vs-instance legality, implicit native bases and dynamic dispatch are not compiled or inferred.",
]


def access_hierarchy(parents, owner):
    """Только известная цепочка; отсутствие базы не означает Class автоматически."""
    seen = set()
    while owner and owner not in seen:
        seen.add(owner)
        yield owner
        owner = parents.get(owner, "")


def access_signature(option):
    return tuple(re.sub(r"\s+", "", parameter["type"]) for parameter in option["params"])


def method_access(methods, parents, receiver_type, caller_type, method_name, argument_count):
    """Проверяет доступ по статическому типу receiver и ближайшим overload signatures.

    Не выбирает overload по типам аргументов. Смешанные/неполные варианты —
    unresolved; finding возникает только когда все применимые варианты запрещены.
    """
    def declared_access(owner, option, trail):
        marker = (owner, id(option))
        if marker in trail:
            return None
        trail = trail | {marker}
        words = option.get("mods", "").split()
        explicit = [word for word in ("private", "protected", "public") if word in words]
        if len(explicit) == 1:
            return explicit[0], owner, option
        if explicit:
            return None
        if "override" not in words:
            return "public", owner, option
        signature = access_signature(option)
        # Предыдущее исходное объявление того же класса в modded-цепочке.
        original = [candidate for candidate in methods.get((owner, method_name), []) if candidate is not option and "override" not in candidate.get("mods", "").split() and access_signature(candidate) == signature]
        if not original:
            for ancestor in list(access_hierarchy(parents, owner))[1:]:
                original = [candidate for candidate in methods.get((ancestor, method_name), []) if access_signature(candidate) == signature]
                if original:
                    values = [declared_access(ancestor, candidate, trail) for candidate in original]
                    break
            else:
                return None
        else:
            values = [declared_access(owner, candidate, trail) for candidate in original]
        if any(value is None for value in values):
            return None
        return values[0] if len({value[:2] for value in values}) == 1 else None

    signatures = {}
    for owner in access_hierarchy(parents, receiver_type):
        local = collections.defaultdict(list)
        for option in methods.get((owner, method_name), []):
            required = sum(not parameter.get("optional", False) for parameter in option["params"])
            if required <= argument_count <= len(option["params"]):
                local[access_signature(option)].append(option)
        for signature, options in local.items():
            if signature not in signatures:
                signatures[signature] = (owner, options)
    if not signatures:
        return {"status": "unresolved", "reason": "method_or_arity_not_resolved", "candidates": []}
    caller_chain = set(access_hierarchy(parents, caller_type))
    results = []
    candidates = []
    for owner, options in signatures.values():
        for option in options:
            inherited = declared_access(owner, option, set())
            if inherited is None:
                results.append("unresolved")
                candidates.append({"owner": owner, "visibility": "unresolved", "path": option.get("path"), "line": option.get("line")})
                continue
            visibility, access_owner, declaration = inherited
            allowed = visibility == "public" or (visibility == "private" and caller_type == access_owner) or (visibility == "protected" and access_owner in caller_chain)
            results.append("allowed" if allowed else "forbidden")
            candidate = {"owner": owner, "visibility": visibility, "access_owner": access_owner, "path": declaration.get("path"), "line": declaration.get("line")}
            if candidate not in candidates:
                candidates.append(candidate)
    if all(result == "forbidden" for result in results):
        return {"status": "forbidden", "reason": "caller_outside_declared_access", "candidates": candidates}
    if all(result == "allowed" for result in results):
        return {"status": "allowed", "reason": "known_access", "candidates": candidates}
    return {"status": "unresolved", "reason": "overload_or_modded_access_ambiguous", "candidates": candidates}


def access_receiver_type(model, position, receiver, caller_type, class_names, fields_by_owner, parents, super_type=None):
    """Поля, параметры и видимые локалы; более поздний local не затеняет ранний вызов."""
    if receiver == "this":
        return caller_type
    if receiver == "super":
        return super_type if super_type is not None else parents.get(caller_type, "")
    offsets = [token.offset for token in model.tokens]
    index = bisect.bisect_right(offsets, position) - 1
    scope_id = model.scope_at(index)
    while scope_id is not None:
        scope = model.scopes[scope_id]
        variables = [variable for variable in scope.variables.get(receiver, []) if variable.token_index <= index]
        if variables:
            return max(variables, key=lambda variable: variable.token_index).type_name
        scope_id = scope.parent
    for owner in access_hierarchy(parents, caller_type):
        if receiver in fields_by_owner.get(owner, {}):
            return fields_by_owner[owner][receiver]
    return receiver if receiver in class_names else ""


def access_argument_count(text, code, opening):
    closing = opening + 1
    nesting = 1
    while closing < len(code) and nesting:
        if code[closing] == "(":
            nesting += 1
        elif code[closing] == ")":
            nesting -= 1
        closing += 1
    return len(split_args(text[opening + 1:closing - 1])) if not nesting else None


def access_self_test():
    """Регрессии доступа и static receiver; исходники и БД не записываются."""
    from enforce_cast_relations import build_model

    methods = collections.defaultdict(list)
    parents = {"Derived": "Root", "Sibling": "Root"}

    def add(owner, name="Test", mods="", parameter_types=(), optional=()):
        methods[(owner, name)].append({"mods": mods, "params": [{"type": type_name, "name": "arg" + str(i), "optional": i in optional} for i, type_name in enumerate(parameter_types)], "path": owner + ".c", "line": len(methods[(owner, name)]) + 1})

    add("Root", mods="protected")
    add("Derived", mods="override")
    add("Root", "Secret", "private")
    add("Root", "Open")
    add("Root", "Overload", "protected", ("int",))
    add("Root", "Overload", "public", ("int", "int"))
    add("Root", "Mixed", "protected", ("int",))
    add("Root", "Mixed", "public", ("string",))
    add("Root", "Optional", "protected", ("int", "int"), (1,))
    add("StaticRoot", mods="protected")
    parents["StaticChild"] = "StaticRoot"
    add("StaticChild", mods="override public")
    add("Modded", mods="protected")
    add("Modded", mods="override")
    cases = [
        ("protected_external", "Root", "Outside", "Test", 0, "forbidden"),
        ("protected_own", "Root", "Root", "Test", 0, "allowed"),
        ("protected_descendant", "Root", "Derived", "Test", 0, "allowed"),
        ("inherited_override_external", "Derived", "Outside", "Test", 0, "forbidden"),
        ("inherited_override_sibling", "Derived", "Sibling", "Test", 0, "allowed"),
        ("private_own", "Root", "Root", "Secret", 0, "allowed"),
        ("private_descendant", "Root", "Derived", "Secret", 0, "forbidden"),
        ("default_public", "Root", "Outside", "Open", 0, "allowed"),
        ("arity_does_not_mask_protected", "Root", "Outside", "Overload", 1, "forbidden"),
        ("public_other_arity", "Root", "Outside", "Overload", 2, "allowed"),
        ("overload_unresolved", "Root", "Outside", "Mixed", 1, "unresolved"),
        ("optional_arity", "Root", "Outside", "Optional", 1, "forbidden"),
        ("unknown_arity", "Root", "Outside", "Test", 3, "unresolved"),
        ("static_receiver_root", "StaticRoot", "Outside", "Test", 0, "forbidden"),
        ("static_receiver_child", "StaticChild", "Outside", "Test", 0, "allowed"),
        ("modded_inherits_access", "Modded", "Outside", "Test", 0, "forbidden"),
    ]
    passed = []
    for label, receiver, caller, name, arity, expected in cases:
        actual = method_access(methods, parents, receiver, caller, name, arity)
        assert actual["status"] == expected, (label, actual)
        passed.append(label)
    names = {"Root", "Derived", "PublicAction", "Caller", "Outside"}
    source = "class Caller { Root action; void F(PublicAction action) { action.Test(); { Root action; action.Test(); } action.Test(); } void G() { { action.Test(); PublicAction action; action.Test(); } action.Test(); } }"
    model = build_model(source, "4_World/Test.c", names | {"void"})
    found = [access_receiver_type(model, match.start(), "action", "Caller", names, {}, parents) for match in re.finditer(r"action\.Test", source)]
    assert found == ["PublicAction", "Root", "PublicAction", "Root", "PublicAction", "Root"], found
    passed.append("receiver_parameter_local_field_shadow_and_order")
    assert access_receiver_type(model, 0, "inherited", "Derived", names, {"Root": {"inherited": "Root"}}, parents) == "Root"
    passed.append("inherited_field_receiver")
    assert access_receiver_type(model, 0, "super", "Derived", names, {}, parents) == "Root"
    assert access_receiver_type(model, 0, "super", "Derived", names, {}, parents, "Derived") == "Derived"
    passed.append("super_and_modded_super_receiver")
    source = '// action.Test();\nstring text = "action.Test();"; /* action.Test(); */'
    assert not re.search(r"action\.Test", masked(source))
    passed.append("comments_strings_masked")
    return {"status": "pass", "tests_passed": len(passed), "tests": passed, "limitations": ACCESS_LIMITATIONS}


def check(dbpath, source, out):
    from enforce_name_collisions import check_source, load_type_index, source_type_declarations
    from enforce_cast_relations import scan_cast_relations
    from enforce_cast_relations import build_model
    from enforce_local_declarations import scan_local_declarations
    from enforce_numeric_context import scan_numeric_context
    from enforce_identifier_bindings import scan_identifier_bindings

    db = read_db(dbpath)
    vanilla_classes = collections.defaultdict(list)
    for row in db.execute("SELECT name,path,modifiers FROM classes WHERE forward=0"):
        vanilla_classes[row["name"]].append(dict(row))
    class_names = {r[0] for r in db.execute("SELECT name FROM classes")}
    parents = {r[0]: r[1] for r in db.execute("SELECT name,base FROM classes WHERE base!=''")}
    aliases = {r[0]: r[1] for r in db.execute("SELECT name,type FROM aliases")}
    methods = collections.defaultdict(list)
    field_types = {}
    for r in db.execute("SELECT * FROM methods"):
        methods[(r["owner"], r["name"])].append({"returns": r["returns_type"], "params": json.loads(r["parameters"]), "mods": r["modifiers"], "path": r["path"], "line": r["line"], "origin": "vanilla"})
    for r in db.execute("SELECT * FROM fields"):
        field_types[(r["owner"], r["name"])] = r["type"]
    parsed = []
    for file in sorted(source.rglob("*.c")):
        text = file.read_text("utf-8-sig")
        record = parse(text, file.relative_to(source).as_posix())
        record["text"] = text
        parsed.append(record)
        for c in record["classes"]:
            class_names.add(c["name"])
            if c["base"]:
                parents[c["name"]] = c["base"]
        for m in record["methods"]:
            methods[(m["owner"], m["name"])].append({**m, "path": record["path"], "origin": "mod"})
        for f in record["fields"]:
            field_types[(f["owner"], f["name"])] = f["type"]
        for a in record["aliases"]:
            aliases[a["name"]] = a["type"]
    findings = []
    unresolved_calls = []
    counts = collections.Counter()
    access_counts = collections.Counter()
    access_unresolved = []
    fields_by_owner = collections.defaultdict(dict)
    for (owner, name), typ in field_types.items():
        fields_by_owner[owner][name] = typ
    known_types = class_names | set(aliases) | {"int", "float", "bool", "string", "vector"}
    def emit(kind, record, line, detail):
        findings.append({"kind": kind, "path": record["path"], "line": line, "detail": detail})
    def hierarchy(owner):
        seen = set()
        while owner and owner not in seen:
            owner = base_type(owner)
            if owner in seen:
                break
            seen.add(owner)
            yield owner
            owner = parents.get(owner, "Class" if owner not in {"Class", "int", "float", "bool", "string", "vector", "typename"} else "")
    def base_type(text):
        name = re.sub(r"\b(?:out|inout|ref|autoptr|notnull|const)\b", "", text).strip().split("<", 1)[0].strip()
        seen = set()
        while name in aliases and name not in seen:
            seen.add(name)
            name = re.sub(r"\b(?:ref|autoptr|const)\b", "", aliases[name]).strip().split("<", 1)[0].strip()
        return name
    def candidates(owner, name):
        return [(ancestor, p) for ancestor in hierarchy(owner) for p in methods[(ancestor, name)]]
    # Типы включают enum/typedef и учитывают слой доступности, а не только
    # classes. RC10.3: поле DamageType пересеклось с ванильным enum из Game.
    namespace = load_type_index(dbpath)
    for record in parsed:
        for name, declaration in source_type_declarations(record["text"], record["path"]):
            if declaration not in namespace.setdefault(name, []):
                namespace[name].append(declaration)
    for record in parsed:
        text = record["text"]
        code = masked(text)
        access_model = build_model(text, record["path"], known_types)
        for collision in check_source(text, record["path"], namespace):
            emit("variable_type_name_collision_review", record, collision["line"], {key: value for key, value in collision.items() if key not in {"path", "line"}})
        for item in record["balance_notes"]:
            emit("balance_review", record, item["line"], item["message"])
        for match in re.finditer(r"\b(?:vector|int|float|string|bool)\s+local\s*(?:=|;|,)", code):
            emit("known_failed_identifier", record, text.count("\n", 0, match.start()) + 1, "Declaration of local: rejected by game compiler in RC7; use a distinct name")
        for c in record["classes"]:
            counts["classes"] += 1
            # Унаследованные типы доступны из следующего модуля, но modded
            # должен изменять класс в модуле его объявления (RC9: HandActionThrow).
            module = re.search(r"(?:^|/)([1-5])_(?:core|gamelib|game|world|mission)(?:/|$)", record["path"], re.I)
            owners = vanilla_classes.get(c["name"], [])
            owner_layers = []
            for declaration in owners:
                layer = re.search(r"(?:^|/)([1-5])_(?:core|gamelib|game|world|mission)(?:/|$)", declaration["path"], re.I)
                if layer:
                    owner_layers.append(int(layer[1]))
            if "modded" in c["mods"].split() and module and owner_layers and int(module[1]) > max(owner_layers):
                emit("modded_module_review", record, c["line"], {"class": c["name"], "module": int(module[1]), "declarations": [d["path"] for d in owners]})
            if c["base"] and c["base"] not in class_names:
                emit("parent_not_in_sources", record, c["line"], c["name"] + " -> " + c["base"])
        for m in record["methods"]:
            counts["methods"] += 1
            if m["name"] in class_names and m["name"] != m["owner"]:
                emit("method_class_name_collision", record, m["line"], m["owner"] + "." + m["name"] + " matches class")
            if "override" in m["mods"].split():
                options = []
                owner = m["owner"]
                seen = set()
                while owner and owner not in seen:
                    seen.add(owner)
                    options.extend(p for p in methods[(owner, m["name"])] if not (p.get("origin") == "mod" and p["path"] == record["path"] and p["line"] == m["line"]))
                    owner = parents.get(owner, "")
                if not options:
                    emit("override_not_in_sources", record, m["line"], m["owner"] + "." + m["name"])
                elif not any(p["returns"] == m["returns"] and [a["type"] for a in p["params"]] == [a["type"] for a in m["params"]] for p in options):
                    emit("override_signature_review", record, m["line"], {"actual": m["signature"], "candidates": [{"returns": p["returns"], "params": p["params"], "path": p["path"], "line": p["line"]} for p in options]})
                else:
                    # В modded-цепочке сохраняем имена аргументов исходного
                    # объявления: иначе игровой компилятор теряет новый идентификатор.
                    declarations = [p for p in options if "override" not in p["mods"].split() and p["returns"] == m["returns"] and [a["type"] for a in p["params"]] == [a["type"] for a in m["params"]]]
                    if declarations and not any([a["name"] for a in p["params"]] == [a["name"] for a in m["params"]] for p in declarations):
                        emit("override_parameter_names_review", record, m["line"], {"actual": m["signature"], "candidates": [{"params": p["params"], "path": p["path"], "line": p["line"]} for p in declarations]})
            if m["body_start"] < 0:
                continue
            body = code[m["body_start"]:m["end"]]
            # Новый метод следующего modded-блока недоступен как метод
            # текущего звена. Проверяем собственные вспомогательные STDV-методы.
            for helper in re.finditer(r"(?<![\w.])\b(STDV_\w+)\s*\(", body):
                declarations = methods[(m["owner"], helper[1])]
                own_block = any(p.get("origin") == "mod" and p["path"] == record["path"] and p.get("class_start") == m["class_start"] for p in declarations)
                if declarations and not own_block and all(p.get("origin") == "mod" for p in declarations):
                    position = m["body_start"] + helper.start()
                    emit("cross_modded_helper_review", record, text.count("\n", 0, position) + 1, {"call": m["owner"] + "." + helper[1], "declarations": [{"path": p["path"], "line": p["line"]} for p in declarations]})
            for call in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", body):
                if call[1] not in class_names:
                    continue
                if call[1] == m["owner"] or re.search(r"\bnew\s*$", body[max(0, call.start()-30):call.start()]):
                    continue
                if any(p["origin"] == "mod" for p in methods[(m["owner"], call[1])]):
                    position = m["body_start"] + call.start()
                    emit("ambiguous_unqualified_call", record, text.count("\n", 0, position) + 1, call[1] + "(...) matches a class and method")
            # Прямые вызовы: типы параметров, полей и простых локальных объявлений.
            # Сложные выражения, typedef и динамические вызовы остаются ручной проверкой.
            variables = {}
            for ancestor in reversed(list(hierarchy(m["owner"]))):
                variables.update(fields_by_owner[ancestor])
            variables.update({p["name"]: p["type"] for p in m["params"]})
            local_rx = re.compile(r"\b(?:ref\s+|autoptr\s+|const\s+)*(?P<type>" + TYPE_RX + r")\s+(?P<name>[A-Za-z_]\w*)\s*(?=[=;,:\)\[])")
            for declaration in local_rx.finditer(body):
                if base_type(declaration["type"]) in known_types:
                    variables[declaration["name"]] = declaration["type"]
            # Игровой компилятор отвергает vector / float. Для направления
            # применяем Normalize(), после чего умножаем на нужную длину.
            for division in re.finditer(r"(?<![\w.])(?P<lhs>[A-Za-z_]\w*)\s*/=?\s*(?P<rhs>[A-Za-z_]\w*|\d+(?:\.\d+)?)", body):
                left_type = base_type(variables.get(division["lhs"], ""))
                right_type = base_type(variables.get(division["rhs"], ""))
                if left_type == "vector" and (right_type in {"int", "float"} or division["rhs"][0].isdigit()):
                    position = m["body_start"] + division.start()
                    emit("vector_scalar_division_review", record, text.count("\n", 0, position) + 1, division.group())
            for call in re.finditer(r"(?<![\w.])(?P<receiver>[A-Za-z_]\w*)\.(?P<method>[A-Za-z_]\w*)\s*\(", body):
                receiver = call["receiver"]
                target_type = base_type(variables[receiver]) if receiver in variables else receiver if receiver in class_names else ""
                if receiver == "this":
                    target_type = m["owner"]
                elif receiver == "super":
                    target_type = m["owner"]
                if not target_type:
                    counts["unresolved_direct_calls"] += 1
                    position = m["body_start"] + call.start()
                    unresolved_calls.append({"path": record["path"], "line": text.count("\n", 0, position) + 1, "receiver": receiver, "method": call["method"]})
                    continue
                if call["method"] in {"Cast", "CastTo"}:
                    counts["casts"] += 1
                    continue
                position = m["body_start"] + call.start()
                call_line = text.count("\n", 0, position) + 1
                options = candidates(target_type, call["method"])
                if not options:
                    emit("direct_method_not_in_index", record, call_line, target_type + "." + call["method"])
                    continue
                counts["resolved_direct_calls"] += 1
                opening = m["body_start"] + call.end() - 1
                closing = opening + 1
                nesting = 1
                while closing < len(code) and nesting:
                    if code[closing] == "(":
                        nesting += 1
                    elif code[closing] == ")":
                        nesting -= 1
                    closing += 1
                actual_count = len(split_args(text[opening + 1:closing - 1]))
                if not any(sum(not p["optional"] for p in option["params"]) <= actual_count <= len(option["params"]) for _, option in options):
                    emit("direct_argument_count_review", record, call_line, {"call": target_type + "." + call["method"], "arguments": actual_count, "candidates": [o.get("signature", o["params"]) for _, o in options]})
            # Отдельный access-pass использует scope-aware static receiver. Он не
            # зависит от плоского legacy variables-map остальных API-проверок.
            owner_class = next((c for c in record["classes"] if c["start"] == m["class_start"]), None)
            super_type = m["owner"] if owner_class and "modded" in owner_class["mods"].split() else parents.get(m["owner"], "")
            for call in re.finditer(r"(?<![\w.])(?P<receiver>[A-Za-z_]\w*)\.(?P<method>[A-Za-z_]\w*)\s*\(", body):
                if call["method"] in {"Cast", "CastTo"}:
                    continue
                position = m["body_start"] + call.start()
                call_line = text.count("\n", 0, position) + 1
                access_counts["direct_calls"] += 1
                target_type = base_type(access_receiver_type(access_model, position, call["receiver"], m["owner"], class_names, fields_by_owner, parents, super_type))
                opening = m["body_start"] + call.end() - 1
                argument_count = access_argument_count(text, code, opening)
                if not target_type or argument_count is None:
                    access_counts["unresolved"] += 1
                    access_unresolved.append({"path": record["path"], "line": call_line, "receiver": call["receiver"], "method": call["method"], "reason": "receiver_or_argument_list_unresolved"})
                    continue
                access = method_access(methods, parents, target_type, m["owner"], call["method"], argument_count)
                access_counts[access["status"]] += 1
                if access["status"] == "forbidden":
                    kind = "protected_access_review" if any(candidate.get("visibility") == "protected" for candidate in access["candidates"]) else "private_access_review"
                    emit(kind, record, call_line, {"call": target_type + "." + call["method"], "caller": m["owner"], "arguments": argument_count, "candidates": access["candidates"]})
                elif access["status"] == "unresolved":
                    access_unresolved.append({"path": record["path"], "line": call_line, "receiver": call["receiver"], "receiver_type": target_type, "method": call["method"], "reason": access["reason"]})
        counts["files"] += 1
    # RC10.4: Cast между Building и BaseBuildingBase отвергнут Enforce.
    # Этот focused checker разрешает только известные простые формы;
    # остальные сохраняет для отдельного чтения, а не считает проверенными.
    cast_report = scan_cast_relations([source], type_db=dbpath, types_from=[source])
    if cast_report.get("errors"):
        raise RuntimeError("Cast checker input errors: " + json.dumps(cast_report["errors"], ensure_ascii=False))
    for collision in cast_report["findings"]:
        relative = Path(collision["path"]).relative_to(source).as_posix()
        findings.append({"kind": "incompatible_cast_type_review", "path": relative, "line": collision["line"], "detail": {key: value for key, value in collision.items() if key not in {"path", "line"}}})
    # RC10.6: foreach с именем unit в sibling if/else отвергнут DayZ.
    # Это проверка повторных declaration tokens, а не изменение block visibility.
    local_report = scan_local_declarations([source], type_db=dbpath, types_from=[source])
    for duplicate in local_report["findings"]:
        relative = Path(duplicate["path"]).relative_to(source).as_posix()
        findings.append({"kind": "duplicate_local_declaration_review", "path": relative, "line": duplicate["line"], "detail": {key: value for key, value in duplicate.items() if key not in {"path", "line"}}})
    for unresolved in local_report["unresolved"]:
        relative = Path(unresolved["path"]).relative_to(source).as_posix()
        findings.append({"kind": "local_declaration_parse_review", "path": relative, "line": unresolved["line"], "detail": {key: value for key, value in unresolved.items() if key not in {"path", "line"}}})
    # RC10.7: '%' внутри float/vector формулы требует отдельного review.
    # Целочисленные часы/индексы не запрещаются и в ModFloat не переводятся.
    numeric_report = scan_numeric_context([source], type_db=dbpath, types_from=[source])
    for numeric in numeric_report["findings"]:
        relative = Path(numeric["path"]).relative_to(source).as_posix()
        findings.append({"kind": "mixed_numeric_modulo_review", "path": relative, "line": numeric["line"], "detail": {key: value for key, value in numeric.items() if key not in {"path", "line"}}})
    # RC10.8: generated local из другого метода не связывает stale use в Launch.
    # Член справа от '.', имя метода и callback не объявляются неизвестным local.
    binding_report = scan_identifier_bindings([source], type_db=dbpath, types_from=[source])
    for binding in binding_report["findings"]:
        relative = Path(binding["path"]).relative_to(source).as_posix()
        findings.append({"kind": "unbound_generated_identifier_review", "path": relative, "line": binding["line"], "detail": {key: value for key, value in binding.items() if key not in {"path", "line"}}})
    out.mkdir(parents=True, exist_ok=False)
    dump(out / "cast_relations.json", cast_report)
    dump(out / "local_declarations.json", local_report)
    dump(out / "numeric_context.json", numeric_report)
    dump(out / "identifier_bindings.json", binding_report)
    dump(out / "access_check.json", {"status": "conflict" if access_counts["forbidden"] else "pass", "counts": dict(access_counts), "unresolved": access_unresolved, "limitations": ACCESS_LIMITATIONS})
    result = {"source": str(source), "database": str(dbpath), "counts": dict(counts), "findings": findings, "enforce_compiled": False, "limitations": "Lexical static checks only; no preprocessing or full type inference. Unresolved symbols require source review."}
    dump(out / "static_check.json", result)
    dump(out / "unresolved_calls.json", unresolved_calls)
    tsv = ["kind\tfile\tline\tdetail"] + ["\t".join(str(v) for v in (f["kind"], f["path"], f["line"], json.dumps(f["detail"], ensure_ascii=False))) for f in findings]
    (out / "FINDINGS.tsv").write_text("\n".join(tsv) + "\n", "utf-8")
    db.close()
    print(json.dumps({"counts": dict(counts), "findings": len(findings), "by_kind": dict(collections.Counter(f["kind"] for f in findings)), "report": str(out)}, ensure_ascii=False, indent=2))
    return result


def gate(dbpath, source, out, reviewed_path):
    result = check(dbpath, source, out)
    reviewed = json.loads(reviewed_path.read_text("utf-8")) if reviewed_path else []
    blocking = []
    for finding in result["findings"]:
        digest = hashlib.sha256((source / finding["path"]).read_bytes()).hexdigest()
        if not any(all(item.get(k) == finding[k] for k in ("kind", "path", "line", "detail")) and item.get("file_sha256") == digest and item.get("reason") for item in reviewed):
            blocking.append(finding)
    summary = {"passed": not blocking, "blocking_findings": blocking, "reviewed_findings": len(result["findings"]) - len(blocking), "enforce_compiled": False}
    dump(out / "gate.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if blocking:
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    b = commands.add_parser("build")
    b.add_argument("--pbo", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--resume-incomplete", action="store_true")
    q = commands.add_parser("query")
    q.add_argument("--db", type=Path, required=True)
    q.add_argument("--name", required=True)
    c = commands.add_parser("check")
    c.add_argument("--db", type=Path, required=True)
    c.add_argument("--source", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    g = commands.add_parser("gate")
    g.add_argument("--db", type=Path, required=True)
    g.add_argument("--source", type=Path, required=True)
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--reviewed", type=Path)
    a = commands.add_parser("self-test-access")
    a.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args.pbo, args.out, args.resume_incomplete)
    elif args.command == "query":
        query(args.db, args.name)
    elif args.command == "check":
        check(args.db, args.source, args.out)
    elif args.command == "gate":
        gate(args.db, args.source, args.out, args.reviewed)
    else:
        result = access_self_test()
        if args.out:
            dump(args.out, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
