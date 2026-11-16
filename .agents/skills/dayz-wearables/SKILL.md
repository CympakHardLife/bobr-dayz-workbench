---
name: dayz-wearables
description: "Разрабатывать одежду DayZ, слоты, защиту, варианты материалов и поведение инвентаря."
---

# Одежда и экипировка

Сверь parent, inventorySlot, cargo/attachments и ground/hands/worn models.
hiddenSelections и texture/material arrays должны соответствовать selections
модели. Не заполняй несовпадающий массив произвольными материалами.

Указатели — api/item.md, inventory.md, damage_health.md. Проверь proxies,
damage zones, вес/вместимость/защиту/намокание по цели; разные персонажи и clipping
требуют визуальной проверки. Линтер не подтверждает посадку модели.

Сценарии: spawn/hands/equip/cargo/attachment/drop/damage/reconnect. Вариант не
должен терять/дублировать карго. Результат — classname/slot/model/variant map,
минимальный diff и проверки. Внешний вид не даёт поручения менять CE/редкость.

Команды вызываются из корня репозитория через `.\bobr.ps1`; точный синтаксис —
`commands` и `<команда> --help`. Общие правила — AGENTS.md.
