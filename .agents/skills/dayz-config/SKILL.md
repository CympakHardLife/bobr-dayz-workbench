---
name: dayz-config
description: "Разрабатывать config.cpp, наследование, CfgPatches/CfgMods и зависимости DayZ по фактическим providers и ресурсам."
---

# Конфиги и зависимости

Найди consumer class и объявляющий его addon. Подтверди parent в source/load order,
а не по одному имени в индексе. requiredAddons указывает CfgPatches provider,
Workshop-папка не равна имени patch. Повторный class может быть расширением:
исследуй owning цепочку, не удаляй совпадения автоматически.

Сверь CfgMods.files, слои, prefix и три пути ресурса: source/stage/PBO. Регистр и
расширение проверяй по consumer contract. lint/resource-audit не вычисляют весь
preprocess/load order. Для бинаризации используй подтверждённый CLI CfgConvert
с output отдельно от источника.

Для совместимости используй dayz-mod-compatibility и реальную загрузку. Опциональная
интеграция не должна стать обязательной зависимостью. Результат — минимальный
diff, provider/resource map, проверки и оставшиеся client/server сценарии.

Команды вызываются из корня репозитория через `.\bobr.ps1`; точный синтаксис —
`commands` и `<команда> --help`. Общие правила — AGENTS.md.
