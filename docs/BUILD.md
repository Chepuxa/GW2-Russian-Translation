# Сборка общего пакета

Игроку нужен готовый ZIP и `Install.cmd`. Для разработки используется один
репозиторий `GW2-Russian-Translation`.

На Windows нужны Python 3.12 x64 и, для пересборки считывателя архива, Rust
MSVC с C++ Build Tools. Создайте отдельное Python-окружение и установите
`components/local-translator/requirements-build.txt`. Для редактуры перевода
достаточно корневого `requirements.txt`, тяжёлые зависимости не нужны.

Из корня репозитория:

```text
python tools/fetch_dependencies.py
powershell -NoProfile -ExecutionPolicy Bypass -File Build-DatTool.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File Build-Module.ps1 -Python python
python tools/catalog.py check
python tools/drafts.py check
python tools/contributions.py check
python tools/glossary.py check
python -m unittest discover -s tests -v
python tools/release.py check
python tools/release.py build
```

Первый шаг загружает конкретные версии DLL, модели и GitHub CLI из `components.lock.json`
и проверяет SHA256. Он не скачивает ветку latest. Сборка модуля сохраняет
хеш его исходников и список файлов runtime в том же lock-файле; изменение
lock-файла должно проверяться в PR вместе с кодом. Изменённый модуль нужно
пересобрать перед сборкой пакета. Для обычной правки перевода пересборка
модуля не требуется.

Результат — `dist/GW2-Russian-Translation.zip` и распакованная папка рядом.
Повторная сборка в непустую папку отказывается перезаписывать её; выберите
новую через `--out dist/имя-новой-сборки`. Корневой `Install.cmd` исходного
проекта использует стандартную собранную папку `dist/GW2-Russian-Translation`.

Бинарные зависимости и модель живут в игнорируемой `.artifacts/`; в Git идут
код, каталог, весь импортированный корпус и кэш, черновики, профиль, лицензии,
шрифт и хеши. Сборщик берёт машинный кэш из `drafts/ru/`, формирует начальную
базу по известным ID и включает их в установку. Ручные reviewed-ID в машинную
базу не включаются. Сборка не читает активную игру и не меняет её файлы.
Условия публикации бинарных зависимостей проверяются отдельно перед публичным
релизом: общая MIT-лицензия не заменяет условия их авторов.

Исходники DLL GlyphCore приватные; она используется как закреплённый бинарный
компонент автора. Подробности — `components/glyphcore/README.md`.

Проверка готовой Windows-сборки в изолированных папках:

```text
python tests/smoke_package.py
python tests/smoke_protocol.py "путь-к-тестовому-модулю-из-отчёта"
```

Первый тест проверяет установку, повторную установку, кэш и ручные исправления,
перенос, конфликт загрузчика и целостность пакета. Второй запускает реальный EXE
на тестовой модели и проверяет ответы протокола; используйте только тестовую
папку из первого отчёта, поскольку модуль сохраняет локальные результаты.
