"""Единый вход Бобр DayZ Workbench. Память Core 1.0 остаётся совместимой.

    bobr.ps1 commands
    bobr.ps1 route --query "Добавить боту повторную реакцию на голос"
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from common import AGENT, VERSION, atomic_json, checked_path, load_config, now, read_json, redact, result, sha, text_file


CORE_COMMANDS = {"doctor", "backends", "search", "context", "recall", "remember", "cache", "verify", "snapshot", "check"}
PIPELINE_COMMANDS = {"preflight", "scaffold", "lint", "build", "inspect-pbo", "deploy"}
DIAGNOSTIC_COMMANDS = {"resource-audit", "layout-audit", "api-snapshot", "api-diff", "logs", "release", "texture", "session-plan", "config-check"}
ALIASES = {
    "/dayz-init": "init", "/dayz-preflight": "preflight", "/dayz-new-mod": "scaffold",
    "/dayz-build-pbo": "build", "/dayz-deploy-pbo": "deploy", "/dayz-lint": "lint",
    "/dayz-route": "route", "/dayz-gate": "gate", "/dayz-health": "health",
    "/dayz-logwatch": "logs", "/dayz-release-pack": "release", "/dayz-server-kit": "release",
    "/dayz-ask": "search", "/dayz-upgrade-guard": "api-diff", "/dayz-panel": "panel",
    "/dayz-pack-texture": "texture", "/dayz-workdrive": "workdrive",
}


def catalog():
    return read_json(AGENT / "workbench/catalog.json")


def roles():
    return read_json(AGENT / "workbench/roles.json")["roles"]


def aliases():
    output = dict(ALIASES)
    for row in catalog()["commands"]:
        for name in row.get("aliases", []):
            output[name.lstrip("/")] = row["name"]
            output["/" + name.lstrip("/")] = row["name"]
    return output


def route(query):
    if not query.strip() or len(query) > 12000:
        raise ValueError("Нужна задача длиной 1..12000 символов")
    lowered = query.lower()
    scored = []
    for row in catalog()["routing"]:
        matches = [word for word in row["keywords"] if word.lower() in lowered]
        if matches:
            scored.append({"role": row["role"], "skills": row["skills"], "matched": matches, "score": len(matches)})
    scored.sort(key=lambda row: (-row["score"], row["role"]))
    if not scored:
        scored = [{"role": "dayz-architect", "skills": ["dayz-orchestrate"], "matched": [], "score": 0}]
    top = scored[:3]
    return {"status": "routed", "role": top[0]["role"], "skills": top[0]["skills"],
            "specialists": top, "method": "deterministic_keywords_with_visible_matches",
            "ambiguous": len(top) > 1 and top[0]["score"] == top[1]["score"],
            "next": "plan_task_and_read_relevant_skill", "agents_spawned": False,
            "host_bridge": "native Codex agent or collaboration.spawn_agent using task brief"}


def invoke_module(module, arguments):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        try:
            code = module.main(arguments)
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else 2
            return code, {"status": "help" if code == 0 else "argument_error", "output": stream.getvalue()}
    value = stream.getvalue()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        payload = {"status": "pass" if code == 0 else "fail", "output": value[-8000:]}
    return code, payload


def core(arguments):
    command = [sys.executable, "-B", "-X", "utf8", str(AGENT / "tools/core/dayz_core.py"),
               "--config", str(AGENT / "core/config.json"), *arguments]
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=180)
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError:
        value = {"status": "pass" if process.returncode == 0 else "fail", "output": process.stdout[-8000:], "error": process.stderr[-2000:]}
    return process.returncode, value


def history(command, payload, config):
    # Храним результаты инструментов, не текст пользовательских запросов/памяти.
    if command in CORE_COMMANDS or command in {"commands", "route", "team", "health", "init", "panel", "task"}:
        return
    if command == "research":
        payload = {"status": payload.get("status", "unknown"), "hits": len(payload.get("hits", [])),
                   "web_sources": len(payload.get("web_sources", [])), "excerpt_chars": payload.get("excerpt_chars"),
                   "query_and_excerpts_stored": False}
    root = checked_path(config["reports_root"])
    root.mkdir(parents=True, exist_ok=True)
    identity = now().replace(":", "").replace(".", "") + "_" + uuid.uuid4().hex[:8]
    def sanitized(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [sanitized(item) for item in value]
        if isinstance(value, dict):
            return {key: sanitized(item) for key, item in value.items()}
        return value
    atomic_json(root / (identity + ".json"), {"command": command, "created_utc": now(), "result": sanitized(payload)})


def health(config):
    missing = []
    for relative in ("workbench/catalog.json", "workbench/roles.json", "core/config.json",
                     "tools/core/dayz_core.py", "tools/memory/context_router.py", "tools/memory/routes.json"):
        if not (AGENT / relative).is_file():
            missing.append(relative)
    try:
        registered_roles = roles()
        for row in registered_roles:
            for relative in [row["profile"], *[".agents/skills/" + name + "/SKILL.md" for name in row["skills"]]]:
                if not checked_path(AGENT / relative).is_file():
                    missing.append(relative)
    except (OSError, ValueError, KeyError, TypeError):
        registered_roles = []
        missing.append("valid_role_registry")
    skill_count = len(list((AGENT / ".agents/skills").glob("*/SKILL.md")))
    native_count = len(list((AGENT / ".codex/agents").glob("*.toml")))
    if not registered_roles or not skill_count or not native_count:
        missing.append("nonempty_skills_and_roles")
    reports = checked_path(config["reports_root"])
    entries = []
    if reports.is_dir():
        for path in sorted(reports.glob("*.json"))[-40:]:
            try:
                value = read_json(path)
                entries.append({"command": value["command"], "created_utc": value["created_utc"],
                                "status": value["result"].get("status", "unknown"), "report": str(path)})
            except (ValueError, OSError, KeyError):
                entries.append({"status": "invalid_report", "report": str(path)})
    runs = []
    store_root = checked_path(config["runs_root"])
    if store_root.is_dir():
        from orchestration import TaskStore
        store = TaskStore(store_root, agent_root=AGENT)
        for path in sorted(store_root.glob("run-*.json"), key=lambda item: item.stat().st_mtime)[-10:]:
            try:
                run = store.show(path.stem)
                counts = {}
                for task in run["tasks"].values():
                    counts[task["status"]] = counts.get(task["status"], 0) + 1
                runs.append({"id": path.stem, "title": run["title"][:180], "counts": counts})
            except (ValueError, OSError, KeyError) as error:
                runs.append({"id": path.stem, "status": "invalid_run", "error": str(error)})
    capabilities = {name: {"status": "available" if value and Path(value).is_dir() else "unavailable",
                           "runtime_validated": False}
                    for name, value in ((key, config.get(key)) for key in ("tools_root", "game_root", "server_root"))}
    return {"status": "degraded" if missing else "ready", "version": VERSION, "recent_checks": entries, "runs": runs,
            "skills": skill_count, "roles": len(registered_roles),
            "native_agent_files": native_count, "missing": sorted(set(missing)), "capabilities": capabilities,
            "runs_root": config["runs_root"], "game_running_checked": False,
            "notice": "Ready относится к локальным инструментам; игровые проверки указаны отдельно."}


def panel(output, config):
    output = checked_path(output)
    if output.suffix.lower() != ".html":
        raise ValueError("Панель сохраняется как HTML")
    status = health(config)
    cards = "".join('<article><b>' + html.escape(row["name"]) + '</b><p>' + html.escape(row["description"]) + '</p><code>.\\bobr.ps1 ' + html.escape(row["name"]) + ' --help</code></article>' for row in catalog()["commands"])
    checks = "".join('<tr><td>' + html.escape(row.get("command", "?")) + '</td><td>' + html.escape(row["status"]) + '</td><td>' + html.escape(row.get("created_utc", "")) + '</td></tr>' for row in status["recent_checks"])
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Бобр DayZ Workbench</title><style>
body{background:#111b21;color:#e1e8eb;font:16px system-ui;margin:0 auto;max-width:1150px;padding:32px}h1{color:#d1ae75}p{line-height:1.5}section{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}article{background:#1c2b34;border:1px solid #3c4b53;border-radius:8px;padding:18px}code{color:#dfc393;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}td{padding:8px;border-bottom:1px solid #3c4b53}button{padding:8px;background:#d1ae75;border:0;border-radius:4px;cursor:pointer}small{color:#a4b5bf}</style>
<h1>Бобр · DayZ Workbench 2.0</h1><p>Локальная рабочая среда: задачи, специалисты, исходники, сборка и доказательства.</p>'''
    page += '<p>' + str(status["skills"]) + ' навыков · ' + str(status["roles"]) + ' ролей · ' + html.escape(now()) + '</p>'
    page += '<p><small>Снимок состояния. Команды копируются в терминал Agent. Страница не запускает процессы; обновление: bobr.ps1 panel.</small></p><h2>Команды</h2><section>' + cards + '</section><h2>Последние проверки</h2><table>' + checks + '</table>'
    page += '<h2>Задания</h2><table>' + ''.join('<tr><td>' + html.escape(row["id"]) + '</td><td>' + html.escape(row.get("title", "")) + '</td><td>' + html.escape(json.dumps(row.get("counts", row.get("status", "unknown")), ensure_ascii=False)) + '</td></tr>' for row in status["runs"]) + '</table>'
    page += '''<script>document.querySelectorAll('code').forEach(c=>{const b=document.createElement('button');b.textContent='Копировать';b.onclick=()=>navigator.clipboard.writeText(c.textContent).then(()=>b.textContent='Скопировано').catch(()=>{const r=document.createRange();r.selectNodeContents(c);const s=window.getSelection();s.removeAllRanges();s.addRange(r);});c.after(b)});</script></html>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return {"status": "pass", "panel": str(output), "kind": "offline_status_and_command_catalog", "process_control": False}


def gate(arguments, config):
    import pipeline
    import diagnostics
    parser = argparse.ArgumentParser(prog="gate")
    parser.add_argument("--source", required=True)
    parser.add_argument("--build-output")
    parser.add_argument("--run", action="store_true", help="Выполнить сборку, если задан build-output")
    args = parser.parse_args(arguments)
    stages = []
    for name, function in (("lint", pipeline.main), ("resource-audit", diagnostics.main)):
        code, value = invoke_module(type("Tool", (), {"main": staticmethod(function)}), [name, "--source", args.source])
        stages.append({"stage": name, "exit_code": code, "result": value})
    format_tool = AGENT / ".agents/skills/dayz-ai-engineering/scripts/enforce_preflight.py"
    process = subprocess.run([sys.executable, "-B", "-X", "utf8", str(format_tool), args.source, "--strict"],
                             capture_output=True, text=True, encoding="utf-8", timeout=90)
    stages.append({"stage": "Enforce_source_form", "exit_code": process.returncode, "output": process.stdout[-5000:]})
    if args.run and all(stage["exit_code"] == 0 for stage in stages):
        cfg_output = str(Path(config["temp_root"]) / ("config-check-" + uuid.uuid4().hex) / "config.bin")
        value = diagnostics.config_check(args.source, cfg_output, config["tools_root"], run=True)
        stages.append({"stage": "CfgConvert", "exit_code": 0 if value["status"] == "pass" else 1, "result": value})
    if args.build_output and all(stage["exit_code"] == 0 for stage in stages):
        build_args = ["build", "--source", args.source, "--output", args.build_output,
                      "--tools", config["tools_root"], "--temp", config["temp_root"]]
        if args.run:
            build_args.append("--run")
        code, value = invoke_module(pipeline, build_args)
        stages.append({"stage": "build", "exit_code": code, "result": value})
    failed = any(stage["exit_code"] != 0 for stage in stages)
    return result("fail" if failed else "pass", [stage["stage"] for stage in stages],
                  skipped=["Enforce_compiler", "runtime_behavior", "visual", "load", "type_gates_require_selected_type_db"], stages=stages)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"--help", "-h"}:
        print("Бобр DayZ Workbench 2.0\nКоманды: commands, init, route, team, task, gate, health, panel, workdrive, editor, ce-audit\n"
              "Pipeline: preflight, scaffold, lint, build, inspect-pbo, deploy\n"
              "Диагностика: resource-audit, layout-audit, api-snapshot, api-diff, logs, release, texture, session-plan\n"
              "Память: " + ", ".join(sorted(CORE_COMMANDS)) + "\nДля аргументов: COMMAND --help; /dayz-* aliases работают в этом CLI.")
        return 0
    command = arguments[0]
    rest = arguments[1:]
    try:
        command = aliases().get(command, command)
        if command in {"commands", "health", "init", "workdrive"} and rest in (["--help"], ["-h"]):
            argparse.ArgumentParser(prog=command, description=catalog()["commands"][next(
                index for index, row in enumerate(catalog()["commands"]) if row["name"] == command)]["description"]).print_help()
            return 0
        config = load_config()
        if command in CORE_COMMANDS:
            code, payload = core([command, *rest])
        elif command in PIPELINE_COMMANDS:
            import pipeline
            if command == "build":
                if "--tools" not in rest: rest.extend(["--tools", config["tools_root"]])
                if "--temp" not in rest: rest.extend(["--temp", config["temp_root"]])
            code, payload = invoke_module(pipeline, [command, *rest])
        elif command in DIAGNOSTIC_COMMANDS:
            import diagnostics
            code, payload = invoke_module(diagnostics, [command, *rest])
        elif command == "task":
            import orchestration
            code, payload = invoke_module(orchestration, ["--store", config["runs_root"], *rest])
        elif command == "research":
            import research
            code, payload = invoke_module(research, rest)
        elif command == "commands":
            payload = {"status": "pass", **catalog(), "aliases": aliases(),
                       "invocation": "bobr.ps1 COMMAND; skills in Codex: $skill-name"}
            code = 0
        elif command == "route":
            parser = argparse.ArgumentParser(prog=command)
            parser.add_argument("--query", required=True)
            payload = route(parser.parse_args(rest).query)
            code = 0
        elif command == "team":
            parser = argparse.ArgumentParser(prog=command)
            parser.add_argument("action", choices=("list", "brief"))
            parser.add_argument("--role")
            args = parser.parse_args(rest)
            selected = roles()
            if args.action == "brief":
                selected = [row for row in selected if row["name"] == args.role]
                if not selected:
                    raise ValueError("Неизвестная роль")
                selected = [{**row, "instructions": text_file(AGENT / row["profile"])} for row in selected]
            payload, code = {"status": "pass", "roles": selected, "processes_started": False}, 0
        elif command == "gate":
            payload = gate(rest, config)
            code = 1 if payload["status"] == "fail" else 0
        elif command in {"health", "init"}:
            payload = health(config)
            code = 0 if payload["status"] == "ready" else 1
            if command == "init":
                payload["next"] = ["preflight", "route --query <задача>", "task create --plan <JSON> --project <source>"]
                payload["workdrive"] = {"path": config["workdrive"], "exists": Path(config["workdrive"]).exists()}
        elif command == "panel":
            parser = argparse.ArgumentParser(prog=command)
            parser.add_argument("--output", default=str(AGENT / "workbench/panel.html"))
            payload, code = panel(parser.parse_args(rest).output, config), 0
        elif command == "workdrive":
            payload = {"status": "pass" if Path(config["workdrive"]).is_dir() else "unavailable",
                       "path": config["workdrive"], "temp_root": config["temp_root"],
                       "mount_or_rename_performed": False, "notice": "Используется настроенный каталог; диски не монтируются и не переименовываются."}
            code = 0 if payload["status"] == "pass" else 1
        elif command == "ce-audit":
            process = subprocess.run([sys.executable, "-B", "-X", "utf8", str(AGENT / "tools/ce-audit/dayz_audit.py"), *rest],
                                     capture_output=True, text=True, encoding="utf-8", timeout=120)
            code = process.returncode
            payload = {"status": "pass" if code == 0 else "fail", "output": process.stdout[-8000:], "error": process.stderr[-2000:]}
        elif command == "editor":
            parser = argparse.ArgumentParser(prog=command)
            parser.add_argument("--kind", choices=("workbench", "objectbuilder", "terrainbuilder"), required=True)
            parser.add_argument("--run", action="store_true")
            args = parser.parse_args(rest)
            candidates = {"workbench": "Bin/Workbench/workbenchApp.exe", "objectbuilder": "Bin/ObjectBuilder/ObjectBuilder.exe", "terrainbuilder": "Bin/TerrainBuilder/TerrainBuilder.exe"}
            tools_root = config.get("tools_root")
            if not tools_root:
                raise ValueError("DayZ Tools не настроены; укажи tools_root при настройке")
            executable = checked_path(Path(tools_root) / candidates[args.kind], exists=True)
            payload, code = {"status": "plan", "executable": str(executable), "started": False}, 0
            if args.run:
                creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                process = subprocess.Popen([str(executable)], cwd=executable.parent, creationflags=creation)
                payload.update(status="started", started=True, pid=process.pid)
        else:
            raise ValueError("Неизвестная команда: " + command + "; смотри commands")
        if payload.get("status") == "help":
            print(payload["output"], end="")
            return code
        history(command, payload, config)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return code
    except FileNotFoundError as error:
        print(json.dumps({"status": "not_configured" if command in {"init", "health"} else "error",
                          "command": command, "error": str(error),
                          "bootstrap_next": "python configure.py"}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "error", "command": command, "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
