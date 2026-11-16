# Venator carrier overlay для space_arena

Статус: **локальный инструмент сборки overlay, runtime не проверен**.
Существующая `scenery/space_arena` (Rebels + Empire) остаётся исходной и не
изменяется. Сторонние mesh, текстуры и layout-файл в git не добавляются:
они лежат во внешнем подготовленном пакете и копируются только в выходной
каталог по явному `--carrier-root`.

## Что делает сборщик

Новые файлы инструмента:

- `tools/build_carrier_arena.py` — сборка overlay;
- `tools/test_build_carrier_arena.py` — stdlib-тесты;
- `docs/CARRIER_ARENA.md` — этот документ.

Сборщик:

1. Читает исходную арену и её `manifest.json` только на чтение.
2. Копирует арену в отдельный `--output-root`.
3. В той же STG-ячейке удаляет **только** placements моделей
   `emp_carrier`, `emp_deck`, `emp_frame`, `emp_rack` (точные XML-пути
   берутся из `manifest.models` исходной арены, а не угадываются).
4. Копирует whitelist внешнего carrier-пакета в
   `Objects/.../Models/venator/` и добавляет одну строку
   `OBJECT_STATIC_AGL Models/venator/carrier.xml` в исходный STG.
5. Сохраняет Rebels placements/poses и все исходные файлы Rebels.
6. Сохраняет исходный `manifest.json` отдельно как `source/manifest.json`
   и пишет новый overlay-manifest со своей схемой
   `bombable.space_arena.overlay.manifest`. Старые хэши и triangle budget
   явно относятся только к `source/manifest.json`; для overlay они
   пересчитаны по фактически записанным `.ac`-файлам.
7. Старые Empire spawn poses/routes/surfaces/stack pairs не выдаются за
   активные: они перенесены в `obsolete` с причиной. Активной Empire
   ground-spawn позы нет.

Carrier ставится в **существующий Empire anchor** исходного manifest.
`--agl-m` по умолчанию `450` — это **design AGL** над рельефом у anchor,
а не измеренная MSL-высота. В STG offset записывается как AGL; в manifest
явно указано `msl_m: null` и `agl_datum:
design_agl_above_terrain_at_empire_anchor`.

## Входной carrier-пакет

`--carrier-root` должен содержать:

```text
carrier.xml
carrier_lod0.ac
carrier_lod1.ac
carrier_lod2.ac
hangar.ac            # только geometry_mode="separate"
layout.json
textures/            # опционально
```

Копируется только этот whitelist. Посторонние файлы/каталоги верхнего
уровня игнорируются и перечисляются в manifest, но в output не попадают.
Симлинки, absolute-пути, `..` и обратные слэши в путях запрещены; XML/AC
ссылки проверяются до записи.

`layout.json` — схема `bombable.carrier-layout/v1`, оси
`X right, Y forward, Z up`:

```json
{
  "schema": "bombable.carrier-layout/v1",
  "axes": "X right, Y forward, Z up",
  "geometry_mode": "unified",
  "bounds_m": [[-12, 12], [-4, 18], [0, 9]],
  "triangles": {"lod0": 7, "lod1": 5, "lod2": 3, "hangar": 0},
  "spawn_slots": [
    {"id": "slot", "position_m": [0, 2, 4.5],
     "heading_deg": 0, "ship_ids": ["ship_id"]}
  ],
  "runtime_verified": false,
  "provenance": {"license_status": "unverified"}
}
```

Числа примера — маленькая тестовая фикстура, не замеры Venator.
`geometry_mode: "unified"` означает цельный корпус с интерьером в LOD-мешах:
`hangar.ac` отсутствует, `triangles.hangar` равен **0**, в XML каждый из трёх
LOD подключён ровно один раз. Это исключает двойную отрисовку ангара.
При `geometry_mode: "separate"` (значение по умолчанию для совместимости)
ангар хранится отдельно в `hangar.ac`, его счётчик положителен.

Для обоих режимов `carrier.xml` проверяется по реальной структуре, а не
только по наличию `<path>`:

- каждый обязательный AC (3 LOD, плюс `hangar.ac` для `separate`) должен
  быть подключён **ровно один раз** собственным прямым дочерним
  `<model><path>` верхнего уровня;
- запрещены несколько `<path>` в одном `<model>`, голый список `<path>` в
  корне, вложенные `<model>`, `<path>` на текстуру и повторные ссылки;
- у каждого `<model>` должно быть непустое `<name>`;
- если объявлены `<animation><type>range</type>`, её `<object-name>` должен
  совпадать с реальным именем object-group в одном из загруженных AC, а
  `<min-m>/<max-m>` должны быть числами; повторный target запрещён.

Это статическая проверка ссылок. Она не доказывает, что FlightGear
нарисует ровно один LOD и что range-пороги подходят: runtime по-прежнему
не проверен.

Проверяются: точные schema/axes, конечные векторы `bounds_m`,
`lod0 > lod1 > lod2 > 0`, совпадение счётчиков с файлами, уникальные `id`, непустые
`ship_ids`, `position_m` внутри bounds, `runtime_verified: false`,
`provenance.license_status` равно `unverified` либо
`source_urls_recorded_terms_pending` (второй статус нормализуется в
manifest в `unverified` с сохранением исходного значения в
`license_status_source`). Остальные поля `layout.json` сохраняются
побайтно.

Первый кандидат layout (вне git, до пересборки цельной модели): 5 слотов с
`heading_deg: 90`, локальные позиции `[20, y, -77]`; в фактическом
`layout.json` на момент проверки `y = -70, -45, -20, 5, 30`. Корпус
длиной 1137 м; 450 м AGL пока design. Подготовленный пакет содержит
боковой ангар и настоящий вырез корпуса; наличие выреза не является
проверкой collision, поддержки палубой или сетевого spawn. Это локальные
координаты носителя, а не глобальная поза FlightGear. Координаты всегда
берутся из файла, а не из текста документа.

В текущем `carrier_package` ещё нет `carrier.xml`; проверка сборки
выполнялась на копии пакета с временно добавленным wrapper по схеме
LOD ниже. Сам `carrier.xml` должен быть добавлен в подготовленный пакет
до запуска инструмента.

На этой копии статический парсер подтвердил заявленные в layout counts:
`lod0=300608`, `lod1=70993`, `lod2=22838`, `hangar=54610`. Overlay
static total (Rebels + lod0 + hangar) = 355710 треугольников. Эти числа
описывают конкретный внешний пакет на момент проверки, а не бюджет
overlay в общем случае. Лимит и исходный total берутся из
`source/manifest.json`; overlay manifest пересчитывает свои счётчики по
фактически записанным `.ac` и не переиспользует исходный бюджет как
свой. Это счётчик геометрии, не FPS и не измерение производительности.

Преобразование локального layout в AC3Db-координаты:

```text
AC = (-Y, Z, -X)
ac_x = -layout_y   (aft)
ac_y =  layout_z   (up)
ac_z = -layout_x   (left)
```

`spawn_slots` в manifest получают дополнительное поле `position_ac_m`.
Публичной команды запуска/ground-spawn нет: слоты — только локальные
метаданные, runtime и поддержка палубой не заявлены.

## Высоты и datum в STG

Строки STG, не относящиеся к carrier, копируются дословно. Сборщик не
подставляет `0.0 AGL` и не переписывает исходный manifest-текст: для
каждого активного placement в overlay manifest сохраняется фактический
`source_stg` с полями `kind`, `lon`, `lat`, `offset_m`, `offset_datum`,
`heading_deg`.

- `OBJECT_STATIC_AGL`: offset — метры над уровнем земли у anchor
  (`agl_offset_m`).
- `OBJECT_STATIC`: offset — высота над уровнем моря по конвенции STG
  (`elevation_msl_m`), это не AGL.

Сводное поле `elevation_definition.active_agl_offset_m` (или
`active_msl_elevation_m`) заполняется только если все активные строки
относятся к одному типу и имеют одно значение. При разных значениях или
типах сводка равна `null`, а `per_placement` содержит фактические числа.
Семантика полей FlightGear в runtime не проверялась.

## Запуск

Из корня репозитория:

```sh
python3 tools/build_carrier_arena.py \
  --carrier-root /path/to/prepared/carrier-pack \
  --output-root /path/to/new-or-empty-overlay

# иные параметры
python3 tools/build_carrier_arena.py \
  --arena-root scenery/space_arena \
  --carrier-root /path/to/prepared/carrier-pack \
  --output-root /path/to/new-or-empty-overlay \
  --agl-m 450
```

`--output-root` должен не существовать или быть пустым, находиться вне
репозитория и не пересекаться с arena/carrier-root. `--arena-root` и
`--carrier-root` также не должны пересекаться ни в одну сторону: иначе
копирование арены захватило бы `private/` и README внешнего пакета.
Сборка идёт в соседний временный каталог и переименовывается атомарно; при
ошибке частичный output не остаётся. Исходная арена и carrier-root не
перезаписываются. Anchor Empire обязан принадлежать той же STG-ячейке,
что и сама STG, иначе сборка отклоняется до копирования.

Выход содержит:

```text
manifest.json                         # overlay manifest
README.md                             # краткая сводка сборки
source/manifest.json                  # исходный manifest, байт-в-байт
source/<stg>.original                 # исходный STG до удаления Empire props
source/README.md
source/VALIDATION.md
source/src/generate_package.py
Objects/.../942042.stg                # Rebels сохранены, Empire props убраны
Objects/.../Models/space_arena/*      # Rebels + неиспользуемые legacy AC
Objects/.../Models/venator/*          # whitelist carrier-пакета
```

Не запускайте `source/src/generate_package.py` внутри output: это
исходный greybox-генератор, он не знает об overlay и перезапишет STG и
manifest старой ареной.

## Проверки

```sh
python3 -m unittest tools/test_build_carrier_arena.py
python3 -m unittest tools/test_space_arena.py
python3 -m unittest tools/test_space_arena_integration.py
```

Тесты overlay используют реальную committed арену и крошечный собственный
carrier-fixture. Проверяются Rebels без изменений, удаление старых Empire
placements, наличие carrier, STG offset/heading, manifest, пересчёт
треугольников и хэшей, отсутствие сторонних файлов, invalid schema/NaN,
дубликаты слотов, traversal/absolute/symlink-ссылки, overlap/non-empty
output и отсутствие частичного output после ошибки. Добавлены регрессии
на структуру XML (ровно один `<model><path>` на AC, без root-списка,
вложенных моделей, path-на-текстуру и дубликатов), на range-targets,
на overlap arena/carrier в обе стороны, на output внутри репозитория, на
anchor вне STG-ячейки, на `5.0` AGL, смешанные offset и `OBJECT_STATIC`.

## LOD, range и culling интерьера (2024.1)

Ниже — проверка по исходникам SimGear tag `2024.1.7`, а не по wiki.
Код моделирования и сам carrier-пакет этот документ не меняет.

Источники:

- `simgear/scene/model/SGReaderWriterXML.cxx`:
  <https://gitlab.com/flightgear/simgear/-/blob/2024.1.7/simgear/scene/model/SGReaderWriterXML.cxx>
  - XML-wrapper читается как PropertyList; `<path>` не обязателен: при его
    отсутствии создаётся пустая main-модель, а вся геометрия может быть в
    дочерних `<model>` (строки ~613–640).
  - дочерние статические модели читаются из `getChildren("model")`, путь —
    из `<path>`, имя группы — из `<name>` (строки ~721–800).
  - `<animation>`-узлы применяются к собранной группе после добавления
    дочерних моделей (строки ~853–888).
- `simgear/scene/model/animation.cxx`:
  <https://gitlab.com/flightgear/simgear/-/blob/2024.1.7/simgear/scene/model/animation.cxx>
  - `SGRangeAnimation` читает `<min-m>`/`<max-m>` (по умолчанию `0` и
    максимум float), создаёт `osg::LOD` и ставит
    `DISTANCE_FROM_EYE_POINT` (строки ~1848–1955).
  - Цель выбирается через `<object-name>`; можно указать несколько
    `<object-name>` в одной анимации.

Важно: для статических дочерних мешей используется **`<model>`**, а не
`<submodel>`: загрузчик берёт именно `getChildren("model")`. `<submodel>`
в этом загрузчике не является контейнером статической геометрии.

### Вариант A: три отдельных AC как дочерние модели

Пример ниже — для старого режима `separate`. Для цельной модели (`unified`)
удаляются `<model>` с `hangar.ac` и range-анимация `Venator_Hangar`: интерьер
уже включён в каждый LOD и не должен подключаться второй раз. Размеры,
позы и счётчики берутся из обновлённого `layout.json`, не из первого кандидата.

Валидная схема (основной `<path>` отсутствует; это предусмотрено
загрузчиком). `<name>` контейнера уникален; `<object-name>` в range
адресует уже именованную группу внутри AC (`Venator_LOD0`,
`Venator_LOD1`, `Venator_LOD2`, `Venator_Hangar`), а не контейнер:

```xml
<?xml version="1.0"?>
<PropertyList>
  <model>
    <name>Venator_LOD0_container</name>
    <path>carrier_lod0.ac</path>
  </model>
  <model>
    <name>Venator_LOD1_container</name>
    <path>carrier_lod1.ac</path>
  </model>
  <model>
    <name>Venator_LOD2_container</name>
    <path>carrier_lod2.ac</path>
  </model>
  <model>
    <name>Venator_Hangar_container</name>
    <path>hangar.ac</path>
  </model>

  <animation>
    <type>range</type>
    <object-name>Venator_LOD0</object-name>
    <min-m>0</min-m>
    <max-m>1500</max-m>
  </animation>
  <animation>
    <type>range</type>
    <object-name>Venator_LOD1</object-name>
    <min-m>1500</min-m>
    <max-m>6000</max-m>
  </animation>
  <animation>
    <type>range</type>
    <object-name>Venator_LOD2</object-name>
    <min-m>6000</min-m>
  </animation>

  <!-- интерьер рисуется только вблизи; порог подобрать по кадру -->
  <animation>
    <type>range</type>
    <object-name>Venator_Hangar</object-name>
    <min-m>0</min-m>
    <max-m>1200</max-m>
  </animation>
</PropertyList>
```

Range-анимация обходит дерево рекурсивно и оборачивает найденный
`<object-name>` в `osg::LOD`; поэтому контейнер и объект лучше называть
по-разному. Если назвать `<model><name>` тем же именем, что и группу в
AC, появятся две вложенные LOD-обёртки с одинаковым диапазоном (двойной
отрисовки не будет, но это лишняя сущность). Имена чувствительны к
регистру. Диапазоны LOD должны быть смежными и не перекрываться; при
точном равенстве на границе поведение OSG лучше проверить в runtime.
`<max-m>` у самого дальнего LOD можно не указывать (по умолчанию —
практически бесконечность).

### Вариант B (не принимается этим сборщиком)

SimGear умеет читать `carrier_lod0.ac` из корневого `<path>` и добавлять
остальные LOD дочерними `<model>`. Этот сборщик такой wrapper отклоняет:
корневой `<path>` смешивается со списком `<model>` и затрудняет
доказательство того, что каждый AC загружен ровно один раз. Для overlay
используется только Вариант A: три (или четыре в `separate`) прямых
дочерних `<model><path>`; `carrier.xml` не содержит корневого `<path>`.

Ограничения:

- Это статическая scenery-модель; range/LOD не создают collision,
  поддержку палубой, FDM или сетевой spawn.
- `range` считается от центра bounding sphere объекта, а не от точки
  входа в ангар; для огромного AC порог 1200 м — только стартовое
  значение.
- Реальная проверка «рисуется ровно один LOD» и culling интерьера
  возможна только в FlightGear 2024.1.7 на host-машине; в этой среде
  FlightGear отсутствует. Сборщик проверяет только структуру ссылок и
  существование именованных групп, но не поведение OSG.

## Происхождение и лицензия

Подготовленный пользователем пакет ссылается на внешние карточки:

- корпус: <https://www.cgtrader.com/items/2582308/download-page>
  (Venator-class Star Destroyer Free, автор dermalt);
- ангар:
  <https://www.cgtrader.com/free-3d-models/space/other/star-wars-destroyer-hangar-f02d7bee-f7f0-4599-b87e-3dc7c4be3f46>.

Эти ссылки и название каталога **не являются доказательством**
коммерческой покупки, бесплатной лицензии или запрета на использование.
Конкретные условия на момент сборки не проверены, поэтому layout обязан
содержать `provenance.license_status: "unverified"` либо
`"source_urls_recorded_terms_pending"` (инструмент нормализует второй
статус в `unverified` в overlay manifest). Не публикуйте output
и не включайте сторонние mesh/текстуры в git до выяснения лицензии.

## Что не заявлено

- загрузка и визуальный результат в FlightGear;
- collision, посадка/поддержка палубой, физика;
- сетевой или авторитетный spawn;
- измеренная MSL-высота или terrain clearance;
- производительность LOD/range и отсутствие popping;
- лицензионная чистота внешнего пакета.
