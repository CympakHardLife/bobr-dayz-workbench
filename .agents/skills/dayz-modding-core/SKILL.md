---
name: dayz-modding-core
description: "Разрабатывать и разбирать моды DayZ по фактическим исходникам, выбирать API и зависимости, проверять сборку и сохранять воспроизводимые результаты. Применять для скриптов, конфигов и ресурсов мода; глубокую логику ботов передавать dayz-ai-engineering."
---

# Ядро моддинга DayZ

Начни с AGENTS.md и текущей задачи. Единый CLI — bobr.py/bobr.ps1, процесс —
[START_HERE](../../../workbench/START_HERE.md). Выбери route и один профильный навык.
Для API сверяй owning scripts, слой/сторону/access и зависимости; индекс не компилятор.
Исследование и проверка: [workflow](references/workflow.md),
[API/build](references/build_api.md), [источники](references/source_reliability.md).
AI — [dayz-ai-engineering](../dayz-ai-engineering/SKILL.md).
Память и cache — [архитектура](../../../core/MEMORY_ARCHITECTURE.md).
Корпуса и модель optional; отсутствие отображается явно. Не обещай provider cache.

## Community Wiki

По теме community-wiki читать docs/WIKI_GUIDE.md: проверенные оговорки и подключение внешнего corpus через core scope wiki. Отделять community reference от owning API и игрового доказательства. Эта справка имеет отдельный CC BY-SA scope; условия — THIRD_PARTY_NOTICES.md.
