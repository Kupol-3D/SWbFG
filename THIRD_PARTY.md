# Лицензии и сторонние исходники

Код Space Arena распространяется на условиях **GPL-2.0-or-later**;
текст GPLv2 — в [LICENSE](LICENSE). Сохранять индивидуальные заголовки файлов.
Это не заявление о правах на Star Wars, сторонние изображения или модели.

## Bombable

- Источник: https://github.com/Aether-FGFS/Bombable-6.0
- Ревизия: `4c3a95d772a3c85a3e921aa2f50482c62fcfcf5a`.
- Неизменный снимок: `vendor/bombable/` (без вложенной Git-истории).
- Лицензия по metadata: GPL version 2 or later;
  оригинал: `vendor/bombable/Docs/COPYING`.
- Авторы по оригинальным metadata: Brent Hugh, Helijah, Aether.

Исходные README, metadata и copyright-заголовки сохранены. Это зависимость,
а не корень Space Arena и не уже интегрированный новый PvP. При упаковке
учитывать условия отдельных ресурсов и предоставление соответствующих
исходников для распространяемого GPL-кода.

## FGMS и Python

FGMS загружается по закреплённой ревизии при сборке `server/Dockerfile`;
его `COPYING` переносится в runtime image. Python-зависимости задаются в
`gateway/requirements*.txt`, а не копируются в Git вместе с virtualenv.
Источник и pin перечислены в `docs/SOURCES.json`.

FlightGear/FGData устанавливаются отдельно. Их отсутствие в этом репозитории
не отменяет лицензионных обязательств при создании общего дистрибутива.
