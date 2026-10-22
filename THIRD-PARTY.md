# Сторонние компоненты

Общая MIT-лицензия относится к нашему коду. Сторонние компоненты сохраняют
своих авторов и условия. Подготовленный локальный пакет предназначен для
переноса этой настройки; его состав и источники перечислены ниже.

| Компонент | Версия / источник | Роль |
| --- | --- | --- |
| Nexus, RaidcoreGG | [2026.2.17.1210](https://github.com/RaidcoreGG/Nexus/releases/tag/v2026.2.17.1210) | Загрузка дополнений |
| Text Translator, ideka | [0.2.0](https://github.com/ideka/text-translator-public/releases/tag/v0.2.0) | Подстановка текста |
| Local Translator, NikeGipple | [0.10.0](https://github.com/NikeGipple/gw2-nexus-local-translator), MIT | Протокол и движок; лицензия в `components/local-translator/source/LICENSE` |
| GlyphCore, nourlie | [0.1.29](https://github.com/nourlie/GlyphCore/releases/tag/v0.1.29) | Отрисовка глифов; DLL автора |
| OPUS-MT, University of Helsinki; INT8-конверсия rohanksaxena | [фиксированная ревизия модели](https://huggingface.co/rohanksaxena/opus-mt-en-ru/tree/3e60bacffde1e346edc65b9d057be7d441872779), CC BY 4.0 | Машинный EN→RU |
| Alegreya Sans Medium, Juan Pablo del Peral / Huerta Tipográfica | [Alegreya Sans](https://github.com/huertatipografica/Alegreya-Sans), OFL 1.1 | Шрифт; лицензия в `assets/fonts/OFL.txt` |
| GitHub CLI | [2.103.0](https://github.com/cli/cli/releases/tag/v2.103.0), MIT | Вход участника и отправка PR; лицензия в runtime `tools/github/LICENSE.txt` |

Публичная часть GlyphCore содержит MIT-лицензию (`components/glyphcore/LICENSE`)
и исходники инструмента архива. Ядро `version-proxy` указано как приватный
[подмодуль](https://github.com/nourlie/GlyphCore/blob/v0.1.29/.gitmodules);
его исходники в этот проект не включены. Nexus и Text Translator также
подключаются официальными бинарными файлами. Их условия не переопределяются
лицензией нашего кода.

CTranslate2 (OpenNMT, MIT), SentencePiece (Google, Apache 2.0), NumPy (BSD)
и остальные библиотеки сохраняют собственные уведомления в runtime `_internal`.
Лицензии и карточка модели из предыдущей настройки сохранены в `licenses/`.
Оригинальные тексты и материалы Guild Wars 2 принадлежат ArenaNet/NCSoft.
