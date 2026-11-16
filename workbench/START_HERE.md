# Начало работы

1. Выполни `python configure.py` с нужным data-root и optional tools/knowledge roots.
2. Проверь `python bobr.py doctor`, `health`, `commands`, затем `route --query <задача>`.
3. Существенную разработку веди через `task create/next/brief/claim/submit/review`.
   Передай brief фактическому помощнику доступным host delegation tool; профили
   .codex/agents — native роли, JSON не исполняет LLM. Продолжение — show/next/reopen.
4. Сборка `build --backend builtin` упаковывает PBO и делает readback, но не компилирует
   Enforce и не подтверждает игру. DayZ Tools backend требует установленного инструмента.
5. Сохрани краткое решение в карточку/experience, затем snapshot/verify.

Точный синтаксис: `<command> --help`, `task --help`. Процесс заданий — TASK_PROTOCOL.md,
поиск — RESEARCH.md. Внешние модели и корпуса необязательны; сеть для research
включается только явным флагом. Deployment targets по умолчанию пусты.

Для полных static gates задай `gate --source ... --type-db ... --types-from ...`.
Проверяй отчёты игровых сценариев через `scenario-results --spec ... --results ...`;
схема, SHA и статусы приведены в [TASK_PROTOCOL.md](TASK_PROTOCOL.md).
