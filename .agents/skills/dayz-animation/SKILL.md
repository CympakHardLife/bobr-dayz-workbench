---
name: dayz-animation
description: "Связывать model.cfg, skeletal/RTM и actions DayZ, устраняя зацикливание и некорректную отмену."
---

# Анимации и действия

Определи владельца: selection transform model.cfg, skeletal/RTM, player action
или script command. Это разные контракты. Найди реальный vanilla action/command
и точную сигнатуру, не подменяй отсутствующий жест похожей анимацией без оговорки.

Запиши start/progress/complete/cancel, урон/смерть, потерю предмета и сетевую
сторону. Изменение состояния выполняется ровно в правильный момент. Сверь
skeleton/bones/selections/proxies/animation sources и phase в доступном source.

Проверяй повтор/прерывание/удаление. Для NPC бинтования нужен приоритет угрозы
и корректный cancel, визуальный skip не исправляет stuck state. Временное
переключение оружия не должно терять предмет. Результат — states/ресурсы/код,
API evidence и отдельно проверенное движение в игре.

Команды вызываются из корня репозитория через `.\bobr.ps1`; точный синтаксис —
`commands` и `<команда> --help`. Общие правила — AGENTS.md.
