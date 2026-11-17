# StarDZ Wiki: подключение и проверенные уроки

Этот документ и карточка `memory/cards/dayzmods-community-wiki.md` распространяются
под [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/legalcode.en).
Адаптация и дополнительные проверки: Cympak, 17.11.2026. Источник и copyright:
StarDZ Team, Copyright (c) 2026, [DayZ-Modding-Wiki](https://github.com/StarDZ-Team/DayZ-Modding-Wiki),
commit `e8d971e3941c274befdd6596baba2e5119efb3e4`. Изменения: русская адресная
инструкция, сокращение и оговорки после сверки с vanilla scripts; исходные главы
не исправлялись. Сохранённое уведомление — [StarDZ-Wiki-LICENCE.txt](../licenses/StarDZ-Wiki-LICENCE.txt).
Корневая proprietary LICENSE Workbench на эти два документа не распространяется.
Это не одобрение нашего проекта авторами wiki.

## Подключение

Работай из корня распакованного Workbench; `$pythonPath` — полный путь к твоему
уже установленному Python, как в INSTALL. Настройщик не скачивает справочник.
Выбери **новый, ещё не существующий** каталог для wiki, вне исходников проекта
либо внутри `.local`. Его родитель должен существовать.

```powershell
$wikiRoot = Join-Path (Get-Location).Path '.local/knowledge/wiki'
New-Item -ItemType Directory -Path (Split-Path $wikiRoot -Parent) -Force | Out-Null
& $pythonPath -B -X utf8 tools/knowledge/import_wiki.py --download --target $wikiRoot
if ($LASTEXITCODE -ne 0) { throw 'Импорт wiki не завершён' }
& $pythonPath -B -X utf8 configure.py --wiki-root $wikiRoot
& $pythonPath -B -X utf8 bobr.py search --scope wiki --query ScriptRPC --budget 2500
& $pythonPath -B -X utf8 bobr.py context --topic community-wiki --scope wiki --query CallLater --budget 5000
& $pythonPath -B -X utf8 bobr.py verify --deep wiki
```

Вместо загрузки можно использовать полученный от автора ZIP:

```powershell
$archivePath = Read-Host 'Абсолютный путь к DayZ-Modding-Wiki-main.zip'
& $pythonPath -B -X utf8 tools/knowledge/import_wiki.py --archive $archivePath --target $wikiRoot
```

Импорт принимает только закреплённые содержимое и SHA файлов. Более новая версия
архива может быть отклонена; это требует новой проверки источника, а не отключения
проверок. Существующий каталог не перезаписывается. `--download` выполняет явный
сетевой запрос; configure/search сами справочник не скачивают.

В индекс входят 105 английских Markdown-глав и README. Исходная LICENCE сохранена
рядом, generic parser не индексирует имя без расширения. Итого 107 выбранных и
106 индексируемых файлов. Переводы, чужие agents/hooks/workflows и программы не
импортируются. Ни один пример из wiki импортёр не запускает.

## Использование знаний

Сначала прочитай адресную оговорку ниже, найди одну главу по английскому API/термину,
затем проверь owning declaration и caller целевой версии игры. Scope `wiki` помечает
материал как community documentation, а не удостоверенный engine API. Выдача
содержит commit, pinned URL и SHA. Бюджет ограничивает символы выдержек.
Core search/context поддерживают `wiki`; у команды research свой список scope,
он этим выпуском не расширяется. Модель для поиска не требуется.

| Тема | Раздел внутри en |
|---|---|
| Типы, modded, lifetime, defaults и overload | 01-enforce-script |
| Слои, split client/server и зависимости | 02-mod-structure |
| Layout, widgets, обработчики | 03-gui-system |
| Аудио и ресурсы | 04-file-formats |
| Конфиги | 05-config-files |
| RPC, actions, timers, звук и AI | 06-engine-api |
| Persistence, events, архитектурные приёмы | 07-patterns |
| Учебные проекты с собственными helpers | 08-tutorials |
| Серверная эксплуатация | 09-server-admin |

## Адресно сверенные выводы

Это выбранные контракты, а не утверждение о проверке каждого примера. Пути ниже
относительны vanilla scripts; сверяй их с актуальным owning source. Дата базы
не доказывает версию установленной игры.

- **RPC:** `3_Game/gameplay.c`, класс ScriptRPC: Send сохраняет буфер до Reset;
  NULL target означает global CGame, NULL recipient — всех клиентов. Ограничение
  получателя не заменяет серверную проверку отправителя, прав и payload.
- **UI:** `5_Mission/GUI/WidgetEventHandler.c`: регистрация ставит SetHandler(this),
  UnregisterWidget очищает карты и снимает handler. Нужен согласованный владелец
  регистрации/cleanup; повторную регистрацию не считать списком слушателей.
- **JSON:** `3_Game/tools/JsonFileLoader.c`: новые LoadFile/SaveFile возвращают
  bool и error; старые JsonLoadFile/JsonSaveFile — void. Сохранившееся значение
  по умолчанию не доказывает успешную загрузку. Схему и диапазоны проверять отдельно.
- **Очереди:** `2_GameLib/tools.c` и `3_Game/DayZGame.c`: CallLater принимает
  param1..param9; очереди тикают в OnUpdate. Delay0 переносит работу, но не делает
  её бесплатной или автоматически фоновой. Нужны отмена, ограничение очереди,
  измерение и разбиение тяжёлой работы. Timer.Run — секунды, CallLater — миллисекунды.
- **Enforce:** `3_Game/tools/InputUtils.c` содержит именованные константы в defaults
  и перегрузки по типам. Поэтому «только literals/NULL» — слишком строгий запрет.
  Native bool map.Insert не раскрывает всю семантику повторного ключа; Set
  документирует upsert. Для нашего кода выбирать явный if/else без тернарного `?:`.
- **Identity:** `Man.GetIdentity()` возвращает объект PlayerIdentity, `GetId()` —
  строку. Сначала проверяется объект, затем строка на пустоту. Отсутствие identity
  само по себе не доказывает, что survivor — бот: важны сторона и lifecycle.
- **AI:** OnNoise в vanilla AIBehaviour закомментирован; это не готовая декларация
  override. Реальный VoN noise создаётся серверным DayZPlayerImplement. Infected
  controller и Human commands — разные ветки. Navmesh не гарантирует body clearance,
  этаж или последовательный осмотр комнат. Команда анимации не выполняет лечение.
- **Двери и погода:** текущий ActionOpenDoors использует DoorManipulationParams,
  caller/directional checks; старого overload мало для оценки механики. Старые
  noise/weather helpers могут быть obsolete; повторное приглушение требует проверки.
- **Звук:** раздел audio противоречит сам себе в запрете расширения `.ogg`.
  Не удалять расширение автоматически. Проверять реальный codec/config/path,
  клиентский playback, parenting и cleanup; громкость/затухание/дождь проверять
  в игре. Один spatial-флаг не подтверждает атмосферную слышимость.

## Границы и права

README upstream не заявляет компиляцию/исполнение всех примеров. В локальном
исследовании были проинвентаризированы 105EN глав и адресно проверены 22 группы
утверждений, местами перекрывающиеся; это не семантический аудит всех code blocks.
Неизвестное native поведение, нагрузка и gameplay требуют отдельных тестов.

Документация corpus и её адаптации — CC BY-SA 4.0; code examples — MIT с полным
notice. Права на donor snippets, изображения и чужие моды проверяются отдельно.
Импортёр оставляет corpus вне Git/ZIP Workbench. Если распространяешь corpus
или индекс с текстами, соблюдай исходные условия. См. [LICENSING.md](LICENSING.md).
