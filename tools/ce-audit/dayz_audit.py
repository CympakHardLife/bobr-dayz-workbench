"""Статическая проверка миссии DayZ без изменения входных файлов.

Запуск: python -B dayz_audit.py --mission FOLDER --output NEWREPORTDIR
Коды выхода: 0 — ошибок нет, 1 — найдены ошибки, 2 — неверные входы.
"""

import argparse
import collections
import csv
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


FIELDS = ("nominal", "min", "lifetime", "restock", "quantmin", "quantmax", "cost")
SELECTORS = ("category", "tag", "usage", "value")
DEFAULTS = {
    "restock_values": [600, 900, 1800, 3600, 7200, 14400],
    "compact_nonpositive": True,
    "stash_prefixes": ["AP_stash_"],
    "forbidden_spawn_classes": ["AKM_TESTBED", "Mag_AKM_Drum_TEST"],
    "max_variants": 10,
    "max_chance_decimals": 2,
    "json_files": [],
}
LIMITATIONS = [
    "Статическая проверка не измеряет FPS, очередь CE, время добычи и поведение после рестарта.",
    "Вместимость карты, географические area flags и реальные свободные точки не проверены.",
    "Совместимость обвесов со слотами и оружием требует отдельного анализа классов/скриптов.",
    "Существование записи types не доказывает загрузку класса в игре или выдачу наград модом.",
    "Шкала restock и компактность записей — политика сборки, а не универсальные требования движка.",
    "Сумма шансов вариантов не обязана быть равна единице; автоматически не нормализуется.",
]


class InputError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sensitive(path):
    for part in Path(path).parts:
        name = part.casefold()
        if name.startswith(".env") or name in {"password", "passwords", "secrets", "credentials"}:
            return True
        if name.endswith((".env", ".key", ".pem", ".pfx", ".p12", ".kdbx", ".rpt", ".log", ".cfg")):
            return True
        if name.startswith(("credentials", "secrets")):
            return True
    return False


class Audit:
    def __init__(self, mission, policy):
        self.mission = mission
        self.policy = policy
        self.reads = {}
        self.errors = []
        self.not_checked = []
        self.scope = "mission"

    def error(self, code, **context):
        self.errors.append({"code": code, "scope": self.scope, **context})

    def read(self, path):
        path = path.resolve()
        if sensitive(path):
            raise InputError("sensitive_input_refused")
        try:
            data = path.read_bytes()
        except OSError:
            raise InputError("input_not_readable") from None
        current = digest(data)
        key = str(path)
        previous = self.reads.get(key)
        if previous is not None and previous != current:
            raise InputError("input_changed_during_read")
        self.reads[key] = current
        return data

    def confined(self, root, relative, code="include_outside_root"):
        raw = Path(relative)
        path = (root / raw).resolve()
        if raw.is_absolute() or not path.is_relative_to(root.resolve()) or sensitive(path):
            self.error(code)
            return None
        return path

    def xml(self, path, label):
        if not path.is_file():
            self.error("missing_xml", file=label)
            return None
        try:
            return ET.fromstring(self.read(path))
        except ET.ParseError as exc:
            self.error("xml_syntax", file=label, line=exc.position[0], column=exc.position[1])
        return None

    def verify_unchanged(self):
        for filename, before in self.reads.items():
            try:
                after = digest(Path(filename).read_bytes())
            except OSError:
                after = None
            if before != after:
                self.error("input_changed_during_audit", file=filename)


def load_json(audit, path):
    def reject_constant(_value):
        raise InputError("nonfinite_json_number")

    try:
        return json.loads(audit.read(path).decode("utf-8-sig"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InputError("invalid_json_input") from None


def validate_policy(policy):
    if not isinstance(policy["compact_nonpositive"], bool):
        raise InputError("invalid_policy_compact_nonpositive")
    for key in ("max_variants", "max_chance_decimals"):
        value = policy[key]
        if type(value) is not int or value < 0 or (key == "max_variants" and value == 0):
            raise InputError("invalid_policy_" + key)
    values = policy["restock_values"]
    if not isinstance(values, list) or not values or any(type(value) is not int or value <= 0 for value in values):
        raise InputError("invalid_policy_restock_values")
    for key in ("stash_prefixes", "forbidden_spawn_classes", "json_files"):
        if not isinstance(policy[key], list) or any(not isinstance(value, str) or not value for value in policy[key]):
            raise InputError("invalid_policy_" + key)


def numbers(audit, element, label, classname):
    values = {}
    for field in FIELDS:
        children = element.findall(field)
        if len(children) > 1:
            audit.error("duplicate_type_field", file=label, classname=classname, field=field)
        if not children:
            continue
        try:
            value = int((children[0].text or "").strip())
            if field not in {"quantmin", "quantmax"} and value < 0:
                raise ValueError
            if field in {"quantmin", "quantmax"} and not -1 <= value <= 100:
                raise ValueError
            values[field] = value
        except ValueError:
            audit.error("invalid_type_number", file=label, classname=classname, field=field)
    return values


def read_types(audit, base, validate=True):
    core_path = audit.confined(base, "cfgeconomycore.xml")
    core = audit.xml(core_path, "cfgeconomycore.xml") if core_path else None
    includes = ["db/types.xml"]
    parsed = {}
    if core is not None:
        if core.tag != "economycore":
            audit.error("unexpected_root", file="cfgeconomycore.xml", expected="economycore")
        for group in core.findall("ce"):
            for entry in group.findall("file"):
                relative = str(Path(group.get("folder", "")) / entry.get("name", ""))
                path = audit.confined(base, relative)
                if path is None:
                    continue
                root = audit.xml(path, relative)
                parsed[relative] = root
                if entry.get("type") == "types":
                    includes.append(relative)
                elif validate and (entry.get("type") == "spawnabletypes" or
                                   (root is not None and root.tag == "spawnabletypes")):
                    audit.not_checked.append("unsupported_extra_spawnable_include:" + relative)
    records = {}
    paths = set()
    for relative in includes:
        path = audit.confined(base, relative)
        if path is None:
            continue
        identity = str(path).casefold()
        if identity in paths:
            audit.error("duplicate_types_include", file=relative)
            continue
        paths.add(identity)
        root = parsed.get(relative)
        if relative not in parsed:
            root = audit.xml(path, relative)
        if root is None:
            continue
        if root.tag != "types":
            audit.error("unexpected_root", file=relative, expected="types")
            continue
        for element in root.findall("type"):
            classname = element.get("name", "").strip()
            key = classname.casefold()
            if not classname:
                audit.error("missing_classname", file=relative)
                continue
            if key in records:
                audit.error("duplicate_classname", classname=classname, file=relative,
                            previous_file=records[key]["file"])
                continue
            values = numbers(audit, element, relative, classname)
            record = {"file": relative, "classname": classname, "values": values,
                      "element": element}
            records[key] = record
            if validate:
                validate_type(audit, record)
    return records


def validate_type(audit, record):
    node, values = record["element"], record["values"]
    context = {"file": record["file"], "classname": record["classname"]}
    nominal, minimum = values.get("nominal", 0), values.get("min", 0)
    if minimum > nominal:
        audit.error("min_exceeds_nominal", **context)
    if values.get("lifetime", 0) <= 0:
        audit.error("lifetime_missing_or_nonpositive", **context)
    flags = node.findall("flags")
    if len(flags) != 1:
        audit.error("flags_missing_or_duplicate", **context)
    for flag in flags:
        if any(value not in {"0", "1"} for value in flag.attrib.values()):
            audit.error("invalid_flag_boolean", **context)
    if nominal > 0:
        if values.get("restock") not in audit.policy["restock_values"]:
            audit.error("policy_restock", **context)
        if record["classname"].casefold() in {
            value.casefold() for value in audit.policy["forbidden_spawn_classes"]
        }:
            audit.error("forbidden_spawn_class", **context)
    compact = {child.tag for child in node} == {"lifetime", "flags"}
    if nominal <= 0 and audit.policy["compact_nonpositive"] and not compact:
        audit.error("policy_nonpositive_not_compact", **context)
    if any(record["classname"].casefold().startswith(prefix.casefold())
           for prefix in audit.policy["stash_prefixes"]) and (nominal > 0 or not compact):
        audit.error("policy_event_stash_not_lifetime_only", **context)


def check_chance(audit, value, context):
    try:
        chance = Decimal(value)
        if not chance.is_finite() or not 0 <= chance <= 1:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        audit.error("invalid_chance", **context)
        return
    digits = max(0, -chance.as_tuple().exponent)
    if digits > audit.policy["max_chance_decimals"] and not 0 < chance < Decimal("0.01"):
        audit.error("policy_chance_precision", **context)


def check_pair(audit):
    roots = {}
    for filename, expected in [("cfgspawnabletypes.xml", "spawnabletypes"),
                               ("cfgrandompresets.xml", "randompresets")]:
        path = audit.confined(audit.mission, filename)
        root = audit.xml(path, filename) if path else None
        if root is not None and root.tag != expected:
            audit.error("unexpected_root", file=filename, expected=expected)
            root = None
        roots[filename] = root
    spawn, presets = roots.values()
    index = {}
    names_casefold = set()
    if presets is not None:
        for node in presets:
            if node.tag not in {"cargo", "attachments"}:
                continue
            key = node.tag, node.get("name", "")
            folded = node.tag, key[1].casefold()
            if not key[1] or folded in names_casefold:
                audit.error("missing_or_duplicate_preset", kind=node.tag, preset=key[1])
            index[key] = node
            names_casefold.add(folded)
    hosts = set()
    groups = []
    if spawn is not None:
        for host in spawn.findall("type"):
            name = host.get("name", "").casefold()
            if not name or name in hosts:
                audit.error("missing_or_duplicate_host", classname=name)
            hosts.add(name)
            groups.extend((group, name, "cfgspawnabletypes.xml") for group in host
                          if group.tag in {"cargo", "attachments"})
    if presets is not None:
        groups.extend((node, node.get("name", ""), "cfgrandompresets.xml") for node in presets
                      if node.tag in {"cargo", "attachments"})
    for group, owner, filename in groups:
        items = group.findall("item")
        context = {"file": filename, "owner": owner, "kind": group.tag}
        if len(items) > audit.policy["max_variants"]:
            audit.error("policy_more_than_max_variants", count=len(items), **context)
        names = [node.get("name", "").casefold() for node in items]
        if "" in names or len(names) != len(set(names)):
            audit.error("missing_or_duplicate_group_item", **context)
        reference = group.get("preset")
        if reference is not None:
            if (group.tag, reference) not in index:
                if (group.tag, reference.casefold()) in names_casefold:
                    audit.error("preset_reference_case_mismatch", preset=reference, **context)
                else:
                    audit.error("missing_preset", preset=reference, **context)
            if items:
                audit.error("mixed_inline_and_preset", **context)
    forbidden = {value.casefold() for value in audit.policy["forbidden_spawn_classes"]}
    for filename, root in roots.items():
        if root is None:
            continue
        for node in root.iter():
            context = {"file": filename, "element": node.tag}
            if "chance" in node.attrib:
                check_chance(audit, node.get("chance"), context)
            if node.tag == "item" and node.get("name", "").casefold() in forbidden:
                audit.error("forbidden_item_reference", classname=node.get("name"), **context)
    return {"hosts": len(hosts), "presets": len(index), "groups": len(groups)}


def check_definitions(audit, records, folder):
    if folder is None:
        audit.not_checked.append("selector_definitions")
        return
    path = audit.confined(folder, "cfglimitsdefinition.xml")
    root = audit.xml(path, "cfglimitsdefinition.xml") if path else None
    if root is None:
        return
    if root.tag != "lists":
        audit.error("unexpected_root", file="cfglimitsdefinition.xml", expected="lists")
        return
    mapping = {"category": "categories", "tag": "tags", "usage": "usageflags", "value": "valueflags"}
    definitions = {}
    for dimension, group in mapping.items():
        definitions[dimension] = {node.get("name") for node in root.findall(f"{group}/{dimension}")}
    aliases = {dimension: {} for dimension in SELECTORS}
    alias_path = audit.confined(folder, "cfglimitsdefinitionuser.xml")
    if alias_path and alias_path.is_file():
        user = audit.xml(alias_path, "cfglimitsdefinitionuser.xml")
        if user is not None and user.tag != "user_lists":
            audit.error("unexpected_root", file="cfglimitsdefinitionuser.xml", expected="user_lists")
            user = None
        if user is not None:
            for dimension, group in mapping.items():
                for alias in user.findall(f"{group}/user"):
                    aliases[dimension][alias.get("name")] = {
                        child.get("name") for child in alias.findall(dimension)
                    }
    for record in records.values():
        for dimension in SELECTORS:
            for node in record["element"].findall(dimension):
                name, alias = node.get("name"), node.get("user")
                context = {"file": record["file"], "classname": record["classname"],
                           "dimension": dimension}
                if name and name not in definitions[dimension]:
                    audit.error("undefined_selector", selector=name, **context)
                if alias:
                    if alias not in aliases[dimension]:
                        audit.error("undefined_user_selector", alias=alias, **context)
                    elif not aliases[dimension][alias] <= definitions[dimension]:
                        audit.error("undefined_alias_member", alias=alias, **context)


def catalog_names(audit, path):
    if path.suffix.casefold() == ".json":
        data = load_json(audit, path)
        if not isinstance(data, list):
            raise InputError("catalog_requires_json_list")
        names = [row.get("classname") if isinstance(row, dict) else row for row in data]
    elif path.suffix.casefold() == ".csv":
        try:
            rows = csv.DictReader(audit.read(path).decode("utf-8-sig").splitlines(), delimiter=";")
            if "classname" not in (rows.fieldnames or []):
                raise InputError("catalog_classname_column_missing")
            names = [row.get("classname") for row in rows]
        except UnicodeDecodeError:
            raise InputError("catalog_not_utf8") from None
    else:
        raise InputError("catalog_requires_csv_or_json")
    if any(not isinstance(name, str) or not name for name in names):
        raise InputError("invalid_catalog_classname")
    return {name.casefold() for name in names}


def budget(records):
    files = collections.defaultdict(lambda: {"types": 0, "positive": 0, "nominal": 0, "min": 0})
    restock = collections.Counter()
    for record in records.values():
        row = files[record["file"]]
        row["types"] += 1
        row["positive"] += int(record["values"].get("nominal", 0) > 0)
        if record["values"].get("nominal", 0) > 0:
            restock[str(record["values"].get("restock", "missing"))] += 1
        for field in ("nominal", "min"):
            row[field] += record["values"].get(field, 0)
    return {"total": {key: sum(row[key] for row in files.values())
                      for key in ("types", "positive", "nominal", "min")},
            "files": dict(files), "restock_distribution": dict(sorted(restock.items()))}


def signature(record):
    node = record["element"]
    return tuple((child.tag, tuple(sorted(child.attrib.items())), (child.text or "").strip())
                 for child in node)


def changes(actual, before):
    result = []
    for key in sorted(actual.keys() | before.keys()):
        new, old = actual.get(key), before.get(key)
        events = []
        if old is None:
            events.append("added")
        elif new is None:
            events.append("removed")
        else:
            if signature(old) != signature(new) or old["file"] != new["file"]:
                events.append("changed")
            was, now = old["values"].get("nominal", 0) > 0, new["values"].get("nominal", 0) > 0
            if was != now:
                events.append("activated" if now else "deactivated")
        for event in events:
            result.append({"change": event, "classname": (new or old)["classname"],
                           "old_file": old["file"] if old else "",
                           "new_file": new["file"] if new else "",
                           "old_nominal": old["values"].get("nominal", 0) if old else "",
                           "new_nominal": new["values"].get("nominal", 0) if new else ""})
    return result


def write_reports(output, report, rows):
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    total = report["budget"]["total"]
    lines = ["# Статическая проверка DayZ", "", f"Дата UTC: {report['created_at_utc']}.",
             f"Общий статус: {report['status']}; ошибок: {len(report['errors'])}.",
             f"Проверяемая миссия: {report['mission_status']}; исходная база: {report['baseline_status']}.",
             f"Types: {total['types']}; со спавном: {total['positive']}; nominal/min: {total['nominal']}/{total['min']}.",
             f"Входы прочитаны без изменения аудитором; контроль хешей: {report['inputs_unchanged']}.",
             "", "## Общий бюджет по подключённым файлам", "",
             "| Файл | Types | Со спавном | Nominal | Min |", "| --- | ---: | ---: | ---: | ---: |"]
    for filename, row in sorted(report["budget"]["files"].items()):
        safe_name = filename.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        lines.append(f"| {safe_name} | {row['types']} | {row['positive']} | {row['nominal']} | {row['min']} |")
    lines += ["", "## Restock положительных записей", "", "| Секунды | Записей |", "| ---: | ---: |"]
    for seconds, count in report["budget"]["restock_distribution"].items():
        lines.append(f"| {seconds} | {count} |")
    lines += ["", "## Проверки", "", "Подробности ошибок и хеши прочитанных входов — report.json.",
             "Изменения относительно базы — changes.csv."]
    for code, count in collections.Counter(row["code"] for row in report["errors"]).items():
        lines.append(f"- {code}: {count}")
    lines += ["", "## Не проверено", ""] + [f"- {item}" for item in report["not_checked"]]
    lines += ["", "## Ограничения", ""] + [f"- {item}" for item in LIMITATIONS]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    fields = ["change", "classname", "old_file", "new_file", "old_nominal", "new_nominal"]
    with (output / "changes.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    for option in ("policy", "baseline", "catalog", "definitions"):
        parser.add_argument("--" + option, type=Path)
    args = parser.parse_args(argv)
    try:
        mission, output = args.mission.resolve(), args.output.resolve()
        baseline = args.baseline.resolve() if args.baseline else None
        definitions = args.definitions.resolve() if args.definitions else None
        roots = [root for root in (mission, baseline, definitions) if root is not None]
        if any(not root.is_dir() or sensitive(root) for root in roots):
            raise InputError("input_directory_invalid")
        if output.exists() or any(output.is_relative_to(root) for root in roots):
            raise InputError("output_must_be_new_and_outside_inputs")
        if sensitive(output):
            raise InputError("sensitive_output_refused")
        audit = Audit(mission, DEFAULTS.copy())
        if args.policy:
            config = load_json(audit, args.policy)
            if not isinstance(config, dict) or set(config) - DEFAULTS.keys():
                raise InputError("invalid_policy_keys")
            audit.policy.update(config)
        validate_policy(audit.policy)
        actual = read_types(audit, mission)
        pair = check_pair(audit)
        check_definitions(audit, actual, definitions)
        if args.catalog:
            known = catalog_names(audit, args.catalog)
            for key, record in actual.items():
                if key not in known:
                    audit.error("classname_outside_catalog", classname=record["classname"], file=record["file"])
        else:
            audit.not_checked.append("catalog_membership")
        audit.not_checked.append("spawnable_catalog_membership")
        for relative in audit.policy["json_files"]:
            if not isinstance(relative, str) or Path(relative).suffix.casefold() != ".json":
                raise InputError("json_allowlist_requires_relative_json_paths")
            path = audit.confined(mission, relative, "json_allowlist_outside_root")
            if path is not None:
                load_json(audit, path)
        rows = []
        before_budget = None
        if baseline:
            audit.scope = "baseline"
            before = read_types(audit, baseline, validate=False)
            before_budget = budget(before)
            rows = changes(actual, before)
            if audit.errors and any(row["scope"] == "baseline" for row in audit.errors):
                audit.not_checked.append("baseline_budget_exact: duplicate/invalid fields use first parsed value, not engine semantics")
            audit.scope = "mission"
        else:
            audit.not_checked.append("baseline_changes")
        audit.verify_unchanged()
        report = {"status": "failed" if audit.errors else "passed", "errors": audit.errors,
                  "mission_status": "failed" if any(row["scope"] == "mission" for row in audit.errors) else "passed",
                  "baseline_status": ("failed" if any(row["scope"] == "baseline" for row in audit.errors) else "parsed") if baseline else "not_checked",
                  "created_at_utc": datetime.now(timezone.utc).isoformat(),
                  "inputs_unchanged": not any(row["code"] == "input_changed_during_audit" for row in audit.errors),
                  "not_checked": audit.not_checked, "budget": budget(actual),
                  "baseline_budget": before_budget, "spawnable": pair,
                  "changes": dict(collections.Counter(row["change"] for row in rows)),
                  "inputs": [{"path": path, "sha256": value} for path, value in sorted(audit.reads.items())],
                  "limitations": LIMITATIONS, "server_started": False}
        write_reports(output, report, rows)
        print(json.dumps({"status": report["status"], "errors": len(audit.errors),
                          "budget": report["budget"]["total"], "report": str(output)}, ensure_ascii=False))
        return 1 if audit.errors else 0
    except (InputError, OSError, TypeError, KeyError) as exc:
        code = str(exc) if isinstance(exc, InputError) else "invalid_or_unreadable_input"
        print(json.dumps({"status": "invalid_input", "code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
