# Локация проекта

`space_arena/` — автономный greybox-пакет TASK4 для FlightGear 2024.1.7.
Его `manifest.json` описывает WGS84-привязку, трансформации, стартовые позы,
маршруты и статус проверки поверхностей. Подробная установка и команды для
конкретных поз находятся в [`space_arena/README.md`](space_arena/README.md).

## Подключение без изменения FGData

Пакет подключается как отдельный корень scenery, содержащий `Objects/`:

```sh
fgfs --fg-root=/path/to/FGData \
  --fg-scenery=scenery/space_arena \
  --aircraft=c172p --airport=KSFO --runway=28R --enable-terrasync
```

Запускать команду следует из корня репозитория либо заменить относительный
путь на абсолютный. TerraSync добавляется FlightGear отдельно; копировать
модели в FGData или скачанную scenery не требуется. Для нескольких корней
`--fg-scenery` используется разделитель ОС (`:` в Unix, `;` в Windows), а их
порядок влияет на приоритет загрузки. Старт KSFO 28R — штатный fallback;
экспериментальные позы у площадок и ограничения описаны в README пакета.

Скачанные terrain/TerraSync и полную базу scenery сюда не добавлять. Изменение
границ районов и координат выполняется через canonical source пакета, а не
ручной правкой только сгенерированных STG/AC-файлов.

## Уровни проверки

```sh
python3 -m unittest tools/test_space_arena.py
python3 -m unittest tools/test_space_arena_integration.py
```

Первый набор проверяет целостность пакета, второй независимо сверяет STG и
XML с контрактом FGData/SimGear release/2024.1. Успех этих проверок не заменяет
реальный запуск FlightGear, визуальную приёмку, измерение terrain clearance
и физический тест поддержки аппарата поверхностями.
