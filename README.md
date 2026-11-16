# Space Arena

Закрытый фанатский командный PvP-прототип для **FlightGear 2024.1.7**:
Rebels против Empire, выбор корабля, атмосферный бой и ограниченный набор
вылетов. Цель матча — 15 уничтожений; правила малых составов описаны в плане.
Проект не является официальным продуктом Lucasfilm.

## Начать работу

1. Установить FlightGear/FGData 2024.1.7 отдельно от Git-репозитория.
2. Подготовить Python и выполнить тесты: [TEAM_ENVIRONMENT](docs/TEAM_ENVIRONMENT.md).
3. Прочитать [статусы и следующие шаги](TASK-HISTORY/README.md),
   [поставленные задачи](TASK/README.md) и [общие интерфейсы](docs/TEAM_INTERFACES.md).
4. Работать в своей ветке, сдавать небольшой PR: [CONTRIBUTING](CONTRIBUTING.md).

## Готовая carrier-арена

`scenery/space_arena_carrier` — готовый вариант арены, где Venator размещён
вместо имперского greybox. Готовые меши и layout поставляются в
каноническом пакете `Aircraft/sa_venator`, а сцена держит только размещение и
связующую ссылку. Blender, `star_destroyer_pack`, локальный `work/runtime` и
внешний carrier-pack для запуска не нужны:

```sh
cd <repo>
fgfs --aircraft=c172p --airport=KSFO --runway=28R \
  --fg-scenery="$PWD/scenery/space_arena_carrier" \
  --fg-aircraft="$PWD/Aircraft"
```

Штатный мир, FGData и земная scenery берутся из обычной установки
FlightGear/TerraSync. Это не телепорт и не готовый спавн на носителе: сцена
только размещает корабль, аппарат стартует в KSFO. Перенос ассета в `Aircraft`
не равен физике или AI: движение корабля — следующий этап и не заявлено.
Готовая сцена не означает полный PvP-релиз; спавн, физика, палуба,
столкновения и целевая приёмка 2024.1.7 ещё не приняты. Подробности —
[CARRIER_ARENA](docs/CARRIER_ARENA.md), правило поставки —
[RELEASE_ASSET_POLICY](docs/RELEASE_ASSET_POLICY.md).

## Состав

| Каталог | Назначение |
|---|---|
| `Aircraft/`, `scenery/` | Корабли, исходная greybox и готовая carrier-арена |
| `gateway/` | Loopback TLS/auth и двунаправленный WS↔UDP relay |
| `server/space/` | Приватная конфигурация FGMS и локальный smoke-override |
| `client/`, `tools/launch.py` | Пример профиля и диагностический c172p launcher |
| `client/space_ui/` | Tk/ttk GUI, демонстрационный каталог и solo-запуск c172p |
| `match_core/` | Детерминированные offline-правила и snapshot-контракт |
| `config/` | Общие примеры; конфиг матча пока design-only |
| `tools/test_*.py` | Автоматические проверки проекта |
| `vendor/bombable/` | Неизменный сторонний add-on с исходниками и лицензией |
| `docs/` | Архитектура, интерфейсы, план и происхождение зависимостей |
| `TASK/`, `TASK-HISTORY/` | Исходные задания, отчёты и открытая приёмка |

## Статус и границы

Это исходная платформа, **не готовая мультиплеерная игра**. Реализованы
greybox арены, диагностический solo/direct launcher, Tk GUI с mock-каталогом,
TLS/auth, локальный WS↔UDP relay и offline-ядро матча.
Полный защищённый маршрут с client companion, публичный deploy, подключение
авторитетного матча и корабли — следующие этапы.
Успех Python-тестов не доказывает запуск FlightGear, Docker или двух игроков.
FGMS UDP не предназначен для публичного доступа без защищённого шлюза.

| Задача | Текущее состояние |
|---|---|
| TASK1 | Не реализована: пакет `sa_xwing` отсутствует |
| TASK2 | `sa_tie` greybox проверен (ограниченно); FDM/сетевой полёт не подтверждены |
| TASK2+ | Прототипный набор TIE-ассетов и статические пакеты `sa_tie_royal_guard` / `sa_tie_m1` / `sa_tie_boarding` существуют; полная in-game/runtime приёмка открыта |
| TASK3 | `sa_fdm_lab` реализован (ограниченный GO с помощниками); окончательный go/no-go у ведущего |
| TASK4 | Готовая carrier-сцена и статические проверки готовы; спавн/физика и целевая runtime-приёмка открыты |
| TASK5 | Локальный relay и loopback-тесты выполнены; настоящий FGMS smoke не проведён |
| TASK6 | GUI и headless-проверки готовы; ручной GUI/FG-запуск не принят |
| TASK7 | Выполнена в пределах offline-ядра; сетевой авторитет вне задачи |

Подробные результаты — [TASK-HISTORY](TASK-HISTORY/README.md). Суффикс
`COMPLETED` у отчёта не означает закрытия перечисленных в нём runtime-блокеров.

- [План месяца](docs/TEAM_PLAN.md)
- [Правила и архитектура](docs/SPACE_ARENA_PLAN.md)
- [Кандидаты кораблей](docs/SHIP_SURVEY.md)
- [Сетевой контракт](docs/SPACE_ARENA_GATEWAY.md)
- [Готовая carrier-арена](docs/CARRIER_ARENA.md)
- [Поставка ассетов](docs/RELEASE_ASSET_POLICY.md)
- [Происхождение и лицензии](THIRD_PARTY.md)

В Git не включаются установленный движок/FGData, скачанная scenery, секреты,
личные профили, агентские журналы и сборочные артефакты. Общие исходные
модели/текстуры допустимы при проверенном происхождении; большие бинарные
ассеты согласуются для Git LFS до первого добавления.
