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

## TIE source models (TASK2+ asset preparation, internal/local)

Исходные STL/архивы для трёх TIE-моделей использованы для локальной подготовки ассетов и генерации статических пакетов; **не публиковались** этим репозиторием. Перед любой публикацией требуется повторная проверка лицензий/условий и прав на IP.

| Ship | Author | Thingiverse URL | Source SHA-256 (STL) | Licence (as stated) |
|---|---|---|---|---|
| TIE Royal Guard (Emperor's Royal Guard TIE Interceptor) | KonbayanSW | https://www.thingiverse.com/thing:4930428 | `353eca3b0e663f1d5459e42d12815ab9d3e8a57d7e49bd057f02d2d84260db13` | Creative Commons — Attribution (CC BY), **exact version unverified** |
| TIE Experimental M1 / Bizarro | KonbayanSW | https://www.thingiverse.com/thing:4884295 | `7c423cc3bcce5bc4aa9549f9f7500414e9f24c83d014277c5520fd9347862788` | Creative Commons — Attribution (CC BY), **exact version unverified** |
| TIE Boarding Craft | KonbayanSW | https://www.thingiverse.com/thing:4909409 | `18f934f7e8eaac0abff20a846baeb2305accbcc12c62224fd5ca54905db520c7` | Creative Commons — Attribution (CC BY), **exact version unverified**; remix source noted in pack docs — **chain/terms outstanding** |

См. также `work/TIE_Game_Asset_Pack_v1/docs/ATTRIBUTION.md`, `docs/SOURCES.md`, `FILES_SHA256.json` и `LICENSE_ATTRIBUTION.md` в сгенерированных `Aircraft/sa_tie_*`.

FlightGear/FGData устанавливаются отдельно. Их отсутствие в этом репозитории
не отменяет лицензионных обязательств при создании общего дистрибутива.

## Venator и ангар: готовые runtime-сетки

`Aircraft/sa_venator/` содержит подготовленные игровые LOD корпуса
и внутреннего ангара (не исходные ZIP/ABC/Blender-файлы). Эти меши не следует
считать GPL-кодом проекта. Источники, авторство, SHA-256 исходников и открытые
вопросы по условиям CGTrader записаны в
[`LICENSE_ATTRIBUTION.md`](Aircraft/sa_venator/LICENSE_ATTRIBUTION.md).
Право на публичную поставку производных сеток пока не подтверждено.

Требование к составу будущего релиза: [политика поставки ассетов](docs/RELEASE_ASSET_POLICY.md).
