# Внешние материалы и зависимости

Bobr DayZ Workbench 2.0.1 содержит собственные инструменты, тесты, профили,
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
- [DayZ Expansion Scripts](https://github.com/ExpansionModTeam/DayZ-Expansion-Scripts).
- [InediaInfectedAI](https://github.com/ysaroka/InediaInfectedAI).
- [Agentic-Z](https://github.com/DayZ-n-Chill/Agentic-Z).
- [Dayz-Modding-Skills](https://github.com/StarDZ-Team/Dayz-Modding-Skills).
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
