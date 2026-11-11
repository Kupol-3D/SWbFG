# TASK7 — отчёт о выполнении: offline scope закрыт

Исходные требования: [TASK7](../TASK/TASK7.md).

## Результат задачи

`match_core/` содержит детерминированный переход состояния по событиям, без
сети, wall-clock и random. Стабильный player_id отделён от callsign;
match/spawn/event IDs защищают от повторного применения. Реализованы team-lock,
каталог команды, одна активная жизнь, бюджет по типам кораблей, подтверждённый
kill и авария без очка, завершение по 15, отдельные fallback-причины для
исчерпания состава и таймлимита. Версии и JSON-примеры событий/snapshot,
сохранение и восстановление описаны в `match_core/CONTRACT.md`.

**Дата аудита/правки:** 2026-11-11. Офлайн-ядро правил матча (`match_core/`)
реализовано и полностью покрыто тестами; по критериям «Готово, когда» TASK7
(все они офлайн-проверяемые) задача закрыта в своём объёме. Runtime-часть
принуждения — отдельные интеграционные задачи CONTRACT, не TASK7.

## Соответствие «Готово, когда» (все пункты имеют тесты)

| Требование | Тест |
|---|---|
| Полный короткий матч | `TestShortMatch` |
| Независимые типы кораблей, чужой корабль | `TestSpawnBudget`, `ship_not_in_team` |
| Reconnect / смена callsign без возврата жизней | `test_reconnect_renames_without_touching_team_budget_or_score`, `TestRestoreReusesTheRules` |
| Повтор смерти / старые spawn / FF / self-kill | `TestDeathScoring`, `stale_spawn`/`friendly_fire`/`self_kill` |
| Запрет второй активной жизни | `spawn_busy` (test_match_core:641,680) |
| 14→15, нет 16-го очка после конца | `TestFifteenKillFixture`, `match_ended` |
| 4×4×3: 15 недостижима без fallback, а не зависание | `TestSmallRosterNeedsAFallback` (`unreachable_target`, exhaustion/time_limit fallback) |
| Snapshot сохраняется/восстанавливается без возврата слотов | `tools/test_match_snapshots.py` (replay-доказательство, анти-фальсификация) |

## Проверено в этой сессии

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/pylibs python3 -m unittest
  tools.test_space_ui tools.test_match_core tools.test_match_snapshots` →
  **Ran 142 tests, OK** (Python 3.11.2; совместимость с целевой 3.14
  этим прогоном не подтверждается).
- Пример из `match_core/README.md` выполнен дословно — проходит.
- Поведенческие зонды вне тестов: dedup (duplicate_event/event_id_conflict) на
  цепочке состояний, bool-как-int в `tick`, `unreachable_target` и оба
  opt-in fallback, `spawn_busy`, crash-цикл без очка, завершение по цели без
  лишних очков — всё соответствует CONTRACT. Дефектов в `match_core/` не
  найдено; три первоначальных «провала» зондов оказались ошибками самих
  зондов (устаревший state / чужой match_id / неверный сценарий respawn), что
  согласуется с чистыми функциями и доверенным порядком событий.
- Регрессия: `TestImportHygiene` в `tools/test_match_core.py` — AST-скан
  `match_core/*.py` на неиспользуемые импорты (тот же класс дефекта, что
  найден в view.py TASK6).

## Файлы

- `tools/test_match_core.py` — `import ast`, helper `unused_imports`,
  `TestImportHygiene` (1 тест). `match_core/` — изменений кода не потребовалось.

## Вне объёма (по границам TASK7, зафиксировано в CONTRACT.md)

- Это не серверное принудление и не античит: вход — доверенные упорядоченные
  события; authenticated ingress, reserve/admit/launch-confirm и rollback
  неудачного старта FG, disconnect/grace, доказательство попаданий — следующие
  интеграционные задачи.
- Сетевой damage-server, gateway/Nasal/FGMS/launcher не трогались.
- Запуск тестов на Python 3.14 и в CI — не выполнялся.
