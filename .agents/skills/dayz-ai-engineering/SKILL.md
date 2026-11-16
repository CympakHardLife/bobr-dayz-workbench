---
name: dayz-ai-engineering
description: "Проектировать и отлаживать собственный Human AI DayZ, исследовать слух, зрение, навигацию, действия и рейды, проверять зависимости и собирать мод по актуальным исходникам. Применять при разработке или разборе AI, а не для обычной экономики CE."
---

# Разработка AI DayZ

Выбери owning API и один изменяемый слой: stimulus → memory → decision → route →
movement/action. Один владелец движения; generation/cancel снимает устаревшие команды.
lastSeen/lastHeard и Y/этаж раздельны. Невидимую цель не сопровождать живой позицией.
Сохраняй threat priority, сроки памяти и причины переключения/отмены.

Проверь navmesh плюс clearance тела, hysteresis, progress/stuck deadline и bounded
recovery; широкий ray не гарантирует валидную траекторию корпуса. Ограничь ray/path
бюджеты на кадр и распределяй сканы между NPC. Actions требуют lifecycle и cleanup,
одна Can-проверка его не создаёт. Door/lock состояние проверять owning API проекта.

До вызова проверь полную сигнатуру, access, типы/наследование, слой/сторону и modded
цепочку. Используй scripts/dayz_script_index.py и sibling gates для статических рисков.
Они не заменяют Enforce. Технические lessons: [reference](references/enforce-compile-lessons.md).
Знания/корпуса: [карта](references/knowledge-map.md). Независимые проверки включают
новый шум, повторный голос, потерю цели, высоту, двери, лестницы, узкие проходы,
отмену, смерть/удаление и нагрузку. Отчёт разделяет static/PBO/Enforce/game.
