# Внешние материалы и зависимости

Bobr DayZ Workbench содержит собственные инструменты, тесты, профили,
карточки и шаблоны. В чистый пакет не включены сторонние корпуса, моды, архивы
toolkit, модели, DLL, игра, сервер или бинарники DayZ Tools. Ссылка или запись
в поисковом allowlist не предоставляет права копировать связанный материал.

## Среда выполнения

- [Python](https://www.python.org/): устанавливается отдельно; условия доступны
  в [официальной лицензии](https://docs.python.org/3/license.html).
- [DayZ и DayZ Tools](https://community.bistudio.com/wiki/DayZ:Modding_Basics):
  внешние продукты Bohemia Interactive; их лицензии и права сохраняются.
  Проект не является официальным продуктом Bohemia Interactive.

## Источники для анализа

Следующие проекты использовались как справочники или исследовались для выбора
архитектуры. Их исходники и управляющие инструкции не входят в этот пакет
как установленные зависимости:

- [Bohemia Interactive DayZ Script Diff](https://github.com/BohemiaInteractive/DayZ-Script-Diff).
- [DayZ Expansion Scripts](https://github.com/salutesh/DayZ-Expansion-Scripts).
- [InediaInfectedAI](https://github.com/ysaroka/InediaInfectedAI).
- [Agentic-Z](https://github.com/DayZ-n-Chill/Agentic-Z).
- [Dayz-Modding-Skills](https://github.com/StarDZ-Team/Dayz-Modding-Skills).
- [DayZ-Modding-Wiki](https://github.com/StarDZ-Team/DayZ-Modding-Wiki).
- [ServerZ](https://github.com/PDTFawks/ServerZ).
- [Enfusion Community Documentation](https://github.com/PDTFawks/enfusion-community-documentation).
- [Enfusion Database Framework](https://github.com/Arkensor/EnfusionDatabaseFramework).
- [PlugMem](https://github.com/TIMAN-group/PlugMem).

Внешние материалы нельзя автоматически считать разрешёнными к переносу в мод.
Перед прямым использованием кода, ассетов или документации нужно проверить
лицензию конкретной версии и сохранить требуемые уведомления авторов. Enfusion,
Reforger и Real Virtuality не заменяют проверку текущего API DayZ.

## Необязательная локальная модель

[llama.cpp](https://github.com/ggml-org/llama.cpp) и GGUF-модель подключаются из
файлов владельца. Они не скачиваются настройщиком и не включаются в архив.
Для [Qwen](https://huggingface.co/Qwen) или другой модели нужно проверить права
именно её версии и сборки. SHA256 подтверждает целостность выбранного файла,
но не предоставляет лицензию на него.

## GitHub CI

Workflow использует внешние
[actions/checkout](https://github.com/actions/checkout) и
[actions/setup-python](https://github.com/actions/setup-python).
Их код загружается runner во время CI и не включён в исходники Workbench.
Оба проекта публикуют собственные условия в upstream LICENSE. CI может получать
Python из сети средствами GitHub; локальный `configure.py` загрузок не выполняет.

## Границы лицензии проекта

[LICENSE](LICENSE) относится к собственным материалам Bobr DayZ Workbench.
Она не меняет прав владельцев внешних продуктов и не присваивает их авторство.
Если будущий релиз пополнится сторонними файлами, перед выпуском нужно добавить
точные notices и условия соответствующих версий.

## Проверка перед следующим выпуском — 17.11.2026

Условия семи непосредственно предложенных GitHub-источников сверены по полным
текстам лицензий закреплённых версий. Перечень версий, правообладателей и SHA
уведомлений — [docs/THIRD_PARTY_REVIEW.json](docs/THIRD_PARTY_REVIEW.json);
правила включения материалов — [docs/LICENSING.md](docs/LICENSING.md).
В2.0.4 добавлены отдельно лицензированные адаптированные wiki-документы и исходное уведомление. Полный corpus по-прежнему не включён.

- **StarDZ Wiki:** документация CC BY-SA 4.0, примеры кода MIT. Копии,
  переводы и адаптации документации требуют атрибуции, ссылки на условия,
  отметки изменений и соблюдения ShareAlike. Их нельзя объявить собственными
  proprietary-материалами только потому, что текст переписан другими словами.
  Corpus не включён в публичный пакет. В2.0.4 адаптированные docs/WIKI_GUIDE.md и
  memory/cards/dayzmods-community-wiki.md выделены под CC BY-SA4.0; исходное
  уведомление сохранено в licenses/StarDZ-Wiki-LICENCE.txt, изменения указаны.
- **Agentic-Z:** собственная source-available лицензия, а не MIT. Она допускает
  бесплатное распространение при сохранении полной лицензии, copyright и ссылки;
  ограничивает коммерциализацию самого Software и запрещает присвоение авторства.
  Его код, prompts, agents, skills и документы в пакет не включаются.
- **Dayz-Modding-Skills, ServerZ, EDF:** MIT допускает включение при сохранении
  полного copyright/permission/disclaimer notice. Для ServerZ правообладатель
  указан как GodBleak; имя владельца fork не заменяет это уведомление.
  У EDF права на значки Freepik/Flaticon проверяются отдельно от кода.
- **Enfusion Community Documentation:** разрешение на распространение исходных
  статей не установлено. Найденная MIT относится к теме Hugo, а не ко всему сайту.
  Оставляем ссылки; текст, переводы и ассеты в пакет не переносим.
- **PlugMem:** корневая Apache-2.0 не заменяет отдельные условия plugin/data.
  При включении кода нужны лицензия, применимые notices и отметки изменений;
  противоречивое MIT-объявление подпроекта и внешние CC BY данные требуют
  отдельной проверки. Эти компоненты и наборы данных в пакет не включены.
- **Expansion Core/AI и Inedia:** также остаются внешними донорами для анализа.
  В проверенных установленных Expansion LICENSE/NOTICE указана CC BY-NC-ND 4.0;
  это не разрешение распространять переработанный код. Для Inedia право на
  перенос исходников/wiki не установлено, описание автора ограничивает repack
  и reupload. Их код, адаптированные реализации и ассеты в Workbench не включаются.

При добавлении чужого файла, выдержки, перевода, адаптированного навыка, модели,
бинарника или поискового индекса с чужим текстом проверка выполняется заново
для точного содержимого исходников и ZIP. Индекс с сохранёнными текстами также
распространяет эти тексты; обозначение его как cache не отменяет условий.
