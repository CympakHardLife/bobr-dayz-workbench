---
name: dayz-weapons
description: "Разрабатывать оружие DayZ, магазины, патроны, режимы огня, отдачу и совместимость обвесов."
---

# Оружие и боеприпасы

Найди parent в api/weapons.md и актуальных config sources. Построй цепочку
weapon → muzzle/mode → magazine → ammo → damage. Проверь slots/chambering,
обвесы, animation/model bindings. Одно classname не доказывает перезарядку.

Отделяй weapon spread от AI perception/aim. Для плохой стрельбы бота сначала
найди владельца разброса и условия fire; изменение ammo затрагивает игроков.
Баланс сравнивай с заданным референсом по единицам и цели.

Сценарии: spawn, chamber/unload, смена магазина, каждый режим, ruin/malfunction,
reconnect. Результат — цепочка классов/providers, ресурсы, значения и отдельная
игровая проверка. Если затронут CE, проверь ammo/magazine доступность через
dayz-economy-check, не меняй общую экономику автоматически.

Команды вызываются из корня репозитория через `.\bobr.ps1`; точный синтаксис —
`commands` и `<команда> --help`. Общие правила — AGENTS.md.
