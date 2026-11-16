# Внешние знания

В пакет не входят исходники игры, сторонние toolkit, модели и чужие моды.
`configure.py --knowledge-root <folder>` задаёт optional corpus root. Для FTS-поиска
нужны records и SQLite индекс engine_library_index.py; одна папка не является
подключённой базой. Doctor показывает фактическую доступность. При отсутствии
корпуса search выдаёт unavailable/ограничение, а не выдуманные результаты.

Проверяй DayZ API по официальным scripts и актуальному установленному снимку.
Основной публичный источник: https://github.com/BohemiaInteractive/DayZ-Script-Diff
Экспериментальный DayZ и Arma Reforger имеют отдельные game scopes. Документация RV3
не доказывает DayZ Enforce API. Сверяй provenance, commit, SHA и лицензию файла.
Expansion, Inedia, Agentic-Z, Dayz-Modding-Skills и memory frameworks могут изучаться
отдельно; они не поставляются и не исполняются этим профилем.

Pinecone, Chroma, Milvus, PlugMem, Mem0 и Letta отключены, адаптеры не реализованы.
Локальная файловая память и optional SQLite FTS5 работают без внешнего API.
Подключение backend требует отдельной реализации, тестов и разрешения на передачу.
