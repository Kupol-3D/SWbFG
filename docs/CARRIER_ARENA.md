# Готовая carrier-арена `space_arena_carrier`

Статус: **готовые меши поставляются в репозитории; это не полный PvP-релиз**.
Канонический пакет носителя — `Aircraft/sa_venator`; сцена
`scenery/space_arena_carrier` хранит только размещение и связующую ссылку на
него. Сейчас это **STATIC_PREVIEW** через STG: перенос ассета в `Aircraft` не
означает подключённую физику, AI или движение. Движение корабля — следующий
этап и не заявлено.

Модель и ангар наблюдались в реальном FlightGear **2024.1.4**; целевая версия
проекта — **2024.1.7**, на ней runtime-приёмка ещё не выполнена. Спавн, физика,
поддержка палубой, столкновения, сетевой матч и переключение LOD не приняты.

Правило поставки ассетов: [RELEASE_ASSET_POLICY](RELEASE_ASSET_POLICY.md).
Пользователь устанавливает FlightGear/FGData, клонирует репозиторий и получает
все нужные игровые файлы; `star_destroyer_pack`, локальный `work/runtime`,
`tie-shared-scenery` и заранее подготовленный внешний carrier-pack для запуска
**не требуются**.

## Что где лежит

`Aircraft/sa_venator/` — канонический пакет корабля:

```text
Models/carrier_lod0.ac
Models/carrier_lod1.ac
Models/carrier_lod2.ac
Models/carrier.xml
Models/layout.json
sa_venator-set.xml          # sim/flight-model=null, alpha visual
README.md
LICENSE_ATTRIBUTION.md
Sources/BUILD_MANIFEST.json
```

`scenery/space_arena_carrier/` — готовый вариант той же арены:

- Rebels-модели и их размещения сохранены;
- имперские greybox-заглушки `emp_carrier`, `emp_deck`, `emp_frame`, `emp_rack`
  удалены из STG; их старые файлы остаются скопированными, но не используются;
- на прежнем Empire anchor (`-122.455000, 37.415000`) стоит STG-размещение
  носителя; высота `450.0` — **design AGL**, а не измеренная MSL-высота;
- `Objects/.../Models/venator/carrier.xml` — только связующий PropertyList:
  `<path>Aircraft/sa_venator/Models/carrier.xml</path>`; копий `.ac`,
  `layout.json` и второго ангара в сцене нет;
- исходная greybox-арена сохранена отдельно и побайтно в `source/` для старых
  детерминированных тестов.

Одновременно подключать `scenery/space_arena` и
`scenery/space_arena_carrier` не нужно: Rebels продублируются. Исходная
`scenery/space_arena` остаётся неизменной и не является обязательным
дополнительным scenery-корнем для готового варианта.

Готовые меши добавлены в git пользователем отдельным ожидаемым commit; его hash
здесь намеренно не фиксируется, источник истины — git. Объём готовых ассетов не
ограничивается малым лимитом: политика прямо разрешает несколько гигабайт.
Проверяется отсутствие исходных архивов, `.blend`, резервных копий и кэшей, а
не общий размер.

## Запуск после клонирования

```sh
cd <repo>
fgfs --aircraft=c172p --airport=KSFO --runway=28R \
  --fg-scenery="$PWD/scenery/space_arena_carrier" \
  --fg-aircraft="$PWD/Aircraft"
```

`--fg-scenery` подключает готовую арену, `--fg-aircraft` делает канонический
пакет `Aircraft/sa_venator` доступным для связующей ссылки сцены. Оба пути —
внутри клона репозитория. Штатный мир, FGData и земная scenery берутся из
обычной установки FlightGear; TerraSync остаётся механизмом FG и не скачивает
модели этой арены. Внешние scenery-корни не обязательны.

Эта команда не телепортирует и не спавнит аппарат на носителе: c172p начинает
на KSFO, а carrier — размещённый сценой объект. Осматривать его нужно полётом
или штатным UFO; публичной команды старта из ангара нет. Подробный вариант
осмотра с UFO — в `README.md` самой сцены и пакета.

## Геометрия и контракт пакета

| LOD | Треугольников |
|---|---:|
| `carrier_lod0.ac` | 371341 |
| `carrier_lod1.ac` | 79857 |
| `carrier_lod2.ac` | 47825 |

- `geometry_mode: "unified"`: интерьер входит в каждый LOD, отдельного
  `hangar.ac` нет, `triangles.hangar = 0`; двойная геометрия ангара исключена.
- `Models/carrier.xml` подключает ровно три LOD как отдельные
  `<model><path>` и задаёт три смежных `range`-анимации по реальным именам
  object-group внутри AC.
- Створки ворот статически открыты в геометрии. Отдельной анимации створок,
  collision-меша и физической палубы нет.
- `Models/layout.json` — схема `bombable.carrier-layout/v1`, оси
  `X right, Y forward, Z up`; bounds и локальные spawn slots конечны,
  `runtime_verified: false`. Слоты — только локальные метаданные, не готовая
  команда спавна.
- `sa_venator-set.xml` — alpha visual viewer: `sim/flight-model` равен `null`,
  ложной физики нет. Мосты к движению/AI появятся отдельным этапом.
- STG-строки Rebels совпадают со старой ареной; имперских placements больше
  нет. Все ресурсы пакета лежат внутри `Aircraft/sa_venator`; сцена ссылается
  только на фиксированный `Aircraft/sa_venator/Models/carrier.xml`.

## Хэши и манифесты

- `Aircraft/sa_venator/Sources/BUILD_MANIFEST.json` — схема
  `bombable.carrier-aircraft-build/v1`; `files` перечисляет SHA-256 всех файлов
  пакета кроме самого manifest; там же `geometry_mode`, фактические
  `triangles`, `model_path: "Models/carrier.xml"` и `runtime_verified: false`.
- `scenery/space_arena_carrier/manifest.json`:
  `carrier_integration.asset_package = "Aircraft/sa_venator"`,
  `layout = "Aircraft/sa_venator/Models/layout.json"`,
  `path_base = "repository-root"`; `models.lod#.file` и `files` используют
  канонические пути от корня репозитория с SHA-256. Это ссылки, а не копии
  файлов внутри сцены.
- `provenance.artifact_sha256` сцены покрывает только собственные файлы сцены
  кроме `manifest.json`.
- `repository_dependencies` сцены фиксирует
  `Aircraft/sa_venator/Sources/BUILD_MANIFEST.json` и его SHA-256, поэтому
  связь сцена ↔ пакет проверяема без внешнего состояния.

## Проверки

Из корня репозитория:

```sh
python3 -m unittest tools/test_carrier_delivery.py
python3 -m unittest tools/test_build_carrier_arena.py
python3 -m unittest tools/test_space_arena.py
python3 -m unittest tools/test_space_arena_integration.py
python3 -m unittest tools/test_repository_hygiene.py
```

`test_carrier_delivery.py` автономен (stdlib, без skip и внешних путей) и
проверяет пакет `Aircraft/sa_venator` отдельно плюс его связь со сценой:

- обязательные 3 LOD, XML, layout, set.xml, README, атрибуция и
  `Sources/BUILD_MANIFEST.json`;
- SHA-256 каждого файла пакета совпадает с `BUILD_MANIFEST.files`, множество
  файлов равно множеству хэшей плюс сам manifest;
- фактическое число треугольников в AC совпадает с `layout.json` и
  `BUILD_MANIFEST.triangles`, `lod0 > lod1 > lod2`;
- `set.xml` не включает ложную физику (`flight-model=null`) и указывает на
  канонический `Aircraft/sa_venator/Models/carrier.xml`;
- схема/оси/конечные bounds/slots, `runtime_verified: false`, unified-режим и
  отсутствие `hangar.ac`;
- XML: три LOD ровно по одному разу, смежные range-анимации по существующим
  группам;
- все ссылки пакета относительны, существуют внутри `Aircraft/sa_venator`, без
  symlink, персональных путей и внешних starter/runtime-зависимостей;
- `source/manifest.json` побайтно равен старому committed manifest, а
  сохранённый исходный STG — старому STG;
- STG: Rebels-строки совпадают со старой ареной минус четыре имперские модели,
  carrier-строка одна и стоит на прежнем anchor;
- в сцене лежит только binder `Models/venator/carrier.xml`, без мешей и layout;
  его единственная ссылка — фиксированный `Aircraft/sa_venator/Models/carrier.xml`;
- `carrier_integration` использует канонические пути от корня репозитория,
  хэши совпадают, `provenance` покрывает только свои файлы сцены,
  `repository_dependencies` фиксирует `Sources/BUILD_MANIFEST.json`;
- в поставке нет исходных архивов, `.blend`, резервных копий и кэшей.

Это статические проверки файлов. Они **не** измеряют FPS, не проверяют
физику, столкновения, загрузку AC в OSG, движение/AI, переключение LOD на
целевом GPU или поведение спавна.

## Происхождение и лицензия

Сторонние меши разрушителя и ангара **не получают лицензию GPL проекта**.
Источники — карточки CGTrader (Venator: автор `dermalt`; ангар: отдельная
free-карточка, автор требует сверки). Условия публичного распространения
производных мешей всё ещё требуют подтверждения; в metadata пакета статус
остаётся `unverified`. Подробности и контрольные суммы исходных authoring-
архивов — в
[`Aircraft/sa_venator/LICENSE_ATTRIBUTION.md`](../Aircraft/sa_venator/LICENSE_ATTRIBUTION.md);
общая политика — [RELEASE_ASSET_POLICY](RELEASE_ASSET_POLICY.md), сторонние
зависимости проекта — [THIRD_PARTY](../THIRD_PARTY.md).

Ограничение не скрывается и не обходится skip-тестами: наличие файлов в
репозитории по решению владельца не доказывает право на публичный релиз.
Star Wars — сторонняя интеллектуальная собственность; проект не является
официальным продуктом Lucasfilm.

## Перегенерация (необязательно)

`tools/build_carrier_arena.py` нужен только для разработки и экспериментов,
а не для запуска сцены. Ему можно передать исходную `scenery/space_arena` и
пакет `Aircraft/sa_venator/Models/` как `--carrier-root`; выход обязан
находиться вне репозитория. Сборщик проверяет unified-режим, XML-структуру,
range-targets, ссылки, STG и layout до записи. Исторический
`source/src/generate_package.py` нельзя запускать поверх готовой сцены: он
перезапишет STG и manifest старой greybox-ареной.

## Что не заявлено

- полный PvP-релиз, готовая игровая сессия или матч;
- движение/AI корабля, спавн/старт TIE из ангара, поддержка палубой и посадка;
- collision, физика, сетевой авторитет;
- загрузка и визуальное поведение на целевом FlightGear 2024.1.7;
- переключение LOD, culling интерьера, FPS/производительность;
- измеренная MSL-высота, terrain clearance и безопасные маршруты;
- лицензионная чистота публичного распространения сторонних мешей.
