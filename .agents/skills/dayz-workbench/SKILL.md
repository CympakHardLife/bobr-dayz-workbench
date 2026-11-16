---
name: dayz-workbench
description: "Настраивать DayZ Workbench/Object Builder и разрабатывать editor tools без смешения с runtime или Reforger API."
---

# Редакторы и editor tools

Определи конкретную утилиту: DayZ Workbench script/UI, Object Builder models,
AddonBuilder packaging. Workbench Reforger имеет другой API. Проверь preflight
и editor help; открытие редактора — отдельное действие, не запуск игры.

Для editor tool найди owning plugin/source установленной ветки. Раздели editor-only
modules и игровой payload, чтобы dependency редактора не ломала dedicated server.
Сначала минимальный вход/выход и отмена массовой операции, потом UI.

Глобальные registry/mount/file associations не меняй ради локального проекта.
Результат — пути/версии, локальная настройка или tool sources, пример результата
и rollback. Открытие EXE подтверждает процесс, а не исправность нового plugin.

Команды вызываются из корня репозитория через `.\bobr.ps1`; точный синтаксис —
`commands` и `<команда> --help`. Общие правила — AGENTS.md.
