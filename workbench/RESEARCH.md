# Проверяемый поиск и необязательная локальная модель

`bobr.py research --query <вопрос> --budget 4000` использует адресную память и Core.
Scope: dayz/dayz-experimental/reforger/toolkits/plugmem; topic выбирает одну карточку.
Без модели — лексический поиск. Отсутствие внешнего FTS корпуса отображается явно.

`--model` включает только разовое планирование 1–3 коротких английских запросов.
Модель и runtime в пакет не входят, скачивания/служб нет. Подключи свои локальные
файлы через configure.py --model <gguf> --llama <completion-exe>; можно задать SHA.
Поддерживаемый runner использует llama-completion с ChatML/JSON grammar, CPU,
4 потока, контекст 2048 и до 160 generated tokens. Другой runtime требует проверки
совместимости. SHA файлов сверяются до inference/cache reuse. Пустой/невалидный
JSON, несовпадение SHA, timeout или отсутствие модели дают lexical fallback с причиной.

Модель получает только текущий короткий вопрос, не весь чат/память, страницы,
архивы и логи. Она не выполняет shell/инструменты и не сохраняет знания. Её identifier
не доказывает API: нужен owning source и проверка версии/стороны.

`--web` без URL возвращает bounded host_web_request и web_queries. Основной агент
может использовать доступный web search tool и передать до 3 выбранных первоисточников
через --web --url <https://...>. В Python встроен GET broker, не поисковик. При
отсутствующем host web tool поиск не выдумывается. Условия host API задаёт приложение.

Broker допускает HTTPS:443 GET без cookies/auth только в configured allowlist.
URL, repository prefix, DNS и каждый redirect проверяются; private IP/userinfo/IP
literals отклоняются. Соединение закреплено на проверенном публичном IP с TLS SNI.
DNS запускается отдельным ограниченным subprocess с kill/wait при deadline; callbacks
resolver не принимаются. Документ до 256KiB, без исполнения HTML и compressed bodies.

Web и query-plan caches находятся в data-root, TTL 24 часа, до 64 entries. Cache
проверяет URL/policy/provenance digest/SHA текста и модель/runtime fingerprints.
model_called=false на cache hit; origin_model_generated отражает происхождение плана,
data_sent=none_cache_reuse — текущий вопрос повторно не отправлен модели.

Общий budget 512–12000 — символы выдержек памяти, кода и страниц, не точные токены.
Ответ содержит provenance, URL/commit/path/line/SHA, предупреждения и ограничения.
Пустая выдача не доказывает отсутствие функции. Проверки работают на изолированных
fixtures без реальной модели/сети; успешные tests не подтверждают DayZ runtime.
