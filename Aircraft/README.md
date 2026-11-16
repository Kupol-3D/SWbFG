# Корабли проекта

Пакеты первой волны: `sa_xwing`, `sa_tie`, испытательный `sa_fdm_lab`.
Они создаются/развиваются задачами TASK1–TASK3 (TASK1 `sa_xwing` по состоянию репозитория не закрыт). Контракт: `docs/TEAM_INTERFACES.md`.

Дополнительно (TASK2+): сгенерированные статические прототипы `sa_tie_royal_guard`, `sa_tie_m1`, `sa_tie_boarding` (из пакета ассетов `work/TIE_Game_Asset_Pack_v1/ships/*/export`), сборка/проверка — `tools/build_tie_packages.py`, сводка — `docs/TIE_INTEGRATION_REPORT.md`. Это Blender MVP + статическая интеграция, не полная in-game приёмка.

Не копировать сюда штатный c172p, полный FGData или сторонние модели без прав.

Разрушитель/носитель: [`sa_venator`](sa_venator/README.md). Готовые сетки и
LOD корпуса/ангара принадлежат этому Aircraft-пакету. Арена
`scenery/space_arena_carrier` содержит только его размещение/ссылку, не вторую
копию меша. Пакет пока визуальный (`flight-model=null`), AI/FDM и игровой
спавн ещё не подключены; перенос в Aircraft не означает готовое движение.
