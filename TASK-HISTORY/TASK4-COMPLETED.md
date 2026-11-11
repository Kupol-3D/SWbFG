# TASK4 — отчёт о реализации greybox арены

**Статус: артефакты и статическая проверка выполнены; исходная runtime-приёмка
не завершена.** Требования: [TASK4](../TASK/TASK4.md).

## Сделано

- Автономный пакет `scenery/space_arena/`, без изменения FGData: Rebels,
  Empire, неподвижный фрагмент носителя и декоративные подвесы.
- Один canonical generator `src/generate_package.py`; собственная геометрия,
  AC3Db, XML с прямым `path`. 9 моделей, 912 треугольников при лимите 25000.
- 10 размещений в `Objects/w130n30/w123n37/942042.stg`, WGS84-координаты,
  трансформации, по primary-позе на фракцию, маршруты и fallback в manifest.
- `OBJECT_STATIC_AGL ... 0.0` используется из-за неизмеренного terrain MSL.
  Это AGL offset, не нулевой MSL. Heading в STG и intended bearing различены.
- Хеши артефактов и генератора, детерминированная регенерация, инструкции
  подключения отдельным scenery-путём. Команда air-start согласована с manifest.

## Проверки

```sh
python3 -m unittest tools.test_space_arena tools.test_space_arena_integration
```

18 тестов проходят: структура AC3Db/XML/STG, независимый расчёт bucket,
бюджет, хеши, воспроизводимость, позы, коридоры и статические AABB.
Эти проверки не доказывают загрузку OSG, terrain clearance или опоры FDM.

## Осталось для полного закрытия

Пройти [VALIDATION](../scenery/space_arena/VALIDATION.md) на FG 2024.1.7:
визуальный масштаб и heading, terrain MSL и отсутствие пересечений,
физическая поддержка обеими палубами, стабильный старт, два клиента,
скриншоты и лог. FG/FGData в контейнере отсутствуют. Палубы `experimental`,
support/collision `unverified`; глобальные столкновения не отключены.
Пока использовать документированный fallback, а не обещать ground-spawn.

Движущийся носитель, большой terrain и финальный арт — вне этого инкремента.
