# Память и контекст

Старт: AGENTS, PROFILE, CURRENT. Затем INDEX и только нужная тема через
`python tools/memory/context_router.py --topic <тема> --read`.
Для code knowledge используй `bobr.py search --help`: внешний индекс необязателен.
Карточки содержат решение, источник, статус, границы и следующий шаг, а не весь чат.
experience.json хранит user_decision/source_confirmed/runtime_confirmed/hypothesis;
исправление явно ссылается на supersedes. Не повышай статический успех до runtime.
После значимой записи выполняй `bobr.py snapshot`, затем `bobr.py verify`.
Checkpoint — ручной allowlist снимок, без автослужб и чтения переписки.
Cache в data-root производный; его очистка не удаляет память и исходники.

2.0.4: тема community-wiki, карточка и docs/WIKI_GUIDE.md включены в ручной snapshot. Corpus хранится отдельно в настроенном wiki-root, не в снимке. Автохуки не добавлены.
