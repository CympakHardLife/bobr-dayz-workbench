---
name: dayz-upgrade-guard
description: "Сравнить snapshots DayZ API и найти затронутые наследования, overrides и callers после обновления исходников."
---

# Проверка изменения API

Используй api-snapshot/api-diff help. Baseline берётся из конкретного source root
с hashes/date/game scope до обновления. Версия базы не доказывает версию игры.
Stable/experimental/Reforger держи отдельно.

Сравни классы/родителей/сигнатуры/guards в пределах parser. Перегрузки должны
сохраняться отдельно. Текстовый parser не моделирует весь preprocessor и active
load order: ограничения явно входят в отчёт.

Свяжи изменения с owning callers/overrides конкретного мода. Не правь все
совпадения имени автоматически. Одинаковая сигнатура не гарантирует поведение:
для важных контрактов сравни body/callers. Результат — snapshots, affected files
и план проверок. После подтверждения обнови нужный индекс и stale cache,
сохрани прежний baseline для сравнения.

Команды вызываются из корня репозитория через `.\bobr.ps1`; точный синтаксис —
`commands` и `<команда> --help`. Общие правила — AGENTS.md.
